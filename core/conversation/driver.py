"""Conversation-mode turn driver (v3 Rollout 2b).

Bridges the pure-logic ConversationEngine to Sapphire's PROVEN turn pipeline:
  on_turn(pcm)   -> STT (whisper) -> process_llm_query (LLM + local streaming TTS)
                    -> wait for playback -> engine.turn_finished()
  on_barge_in()  -> cancel_generation() + tts.stop()

The driver OWNS the engine; the audio front-door (local sounddevice+DTLN, or the
browser intake) just calls `driver.push_frame(pcm, is_speech)`. STT is injectable
(`transcribe_fn`) so this is unit-testable without whisper or an LLM.

Reuses, not reinvents: `process_llm_query` already does text -> LLM -> local
streaming TTS with a processing lock; `cancel_generation` + `tts.stop` are the
Mjolnir-proven interrupt path. Conversation mode is just a new trigger for them.
"""
import logging
import os
import tempfile
import threading
import wave

from core.conversation.engine import ConversationEngine

logger = logging.getLogger(__name__)


class ConversationDriver:
    def __init__(self, system, transcribe_fn=None, sample_rate=16000, **engine_kw):
        self.system = system
        self.sample_rate = sample_rate
        self._transcribe_fn = transcribe_fn or self._whisper_transcribe
        self.engine = ConversationEngine(
            on_turn=self._on_turn,
            on_barge_in=self._on_barge_in,
            sample_rate=sample_rate,
            **engine_kw,
        )

    # ── front-door entry ────────────────────────────────────────────────────
    def push_frame(self, pcm, is_speech):
        """Feed one cleaned audio frame + its VAD decision into the engine."""
        self.engine.push_frame(pcm, is_speech)

    def reset(self):
        self.engine.reset()

    # ── engine callbacks ────────────────────────────────────────────────────
    def _on_turn(self, pcm):
        # Non-blocking: the audio path must not wait on STT/LLM/TTS.
        self._spawn(self._run_turn, pcm)

    def _on_barge_in(self):
        # Must be quick — just interrupt. The engine is already capturing the
        # barge-in utterance.
        try:
            self.system.cancel_generation()
        except Exception as e:
            logger.warning(f"[CONV] barge-in cancel_generation failed: {e}")
        try:
            self.system.tts.stop()
        except Exception as e:
            logger.warning(f"[CONV] barge-in tts.stop failed: {e}")

    # ── the turn ────────────────────────────────────────────────────────────
    def _run_turn(self, pcm):
        try:
            text = self._transcribe_fn(pcm)
            if text and text.strip():
                logger.info("[CONV] turn: transcribed user utterance")
                self.system.process_llm_query(text)
                # process_llm_query returns once the LLM is done and tts.speak() is
                # kicked off (speak is non-blocking). Wait for playback to actually
                # START, then FINISH — so we stay in RESPONDING (barge-in-able) for the
                # whole spoken reply rather than racing to IDLE before she's done.
                self._wait_for_playback()
            else:
                logger.info("[CONV] turn: no usable speech, skipping LLM")
        except Exception as e:
            logger.error(f"[CONV] turn failed: {e}")
        finally:
            # No-op if a barge-in already moved us out of RESPONDING.
            self.engine.turn_finished()

    def _wait_for_playback(self, start_timeout=2.0, max_play=180):
        """Hold the turn in RESPONDING for the spoken reply: wait for tts playback
        to START (it's kicked off non-blocking), then for it to FINISH."""
        import time
        tts = getattr(self.system, "tts", None)
        if tts is None:
            return
        deadline = time.monotonic() + start_timeout
        while not getattr(tts, "_is_playing", False) and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            tts.wait(timeout=max_play)
        except Exception:
            pass

    # ── helpers ─────────────────────────────────────────────────────────────
    def _spawn(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def _whisper_transcribe(self, pcm):
        """Write the endpointed int16 PCM to a temp 16k mono wav and transcribe
        via the existing whisper provider (the same `transcribe_file` the
        wakeword path uses). Stdlib `wave` — no extra deps."""
        wc = getattr(self.system, "whisper_client", None)
        if wc is None:
            return None
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.sample_rate)
                w.writeframes(pcm)
            return wc.transcribe_file(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
