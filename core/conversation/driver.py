"""Conversation-mode turn driver (v3 Rollout 2b — STREAMING).

Bridges the pure-logic ConversationEngine to Sapphire's streaming pipeline:
  on_turn(pcm)   -> STT (whisper) -> drive chat_stream():
                      content   -> event bus (VOICE_TURN_CHUNK)  [web UI streams in]
                      tts_chunk -> PumpkinChunker                [local audio streams out]
                    -> wait for audio to finish -> engine.turn_finished()
  on_barge_in()  -> cancel_generation() (halts chat_stream) + sink.stop() (cuts audio)

Routing voice through chat_stream (instead of the blocking chat()) is what makes
the reply stream to the UI AND makes the LLM cancellable for real barge-in.

NOTE: local audio depends on TTS streaming being enabled (chat_stream's pump only
emits tts_chunk events then). With it off, the UI still streams but she's silent —
a fallback (tts.speak final) is a later add. STT/stream/sink are injectable for tests.
"""
import logging
import os
import tempfile
import threading
import uuid
import wave

from core.conversation.engine import ConversationEngine
from core.event_bus import publish, Events

logger = logging.getLogger(__name__)


class ConversationDriver:
    def __init__(self, system, transcribe_fn=None, sink_factory=None,
                 sample_rate=16000, **engine_kw):
        self.system = system
        self.sample_rate = sample_rate
        self._transcribe_fn = transcribe_fn or self._whisper_transcribe
        self._sink_factory = sink_factory          # injectable; default = PumpkinChunker
        self._sink = None
        self._active_sink = None                   # set during a turn so barge-in can reach it
        self.engine = ConversationEngine(
            on_turn=self._on_turn,
            on_barge_in=self._on_barge_in,
            sample_rate=sample_rate,
            **engine_kw,
        )

    # ── front-door entry ────────────────────────────────────────────────────
    def push_frame(self, pcm, is_speech):
        self.engine.push_frame(pcm, is_speech)

    def reset(self):
        self.engine.reset()

    # ── engine callbacks ────────────────────────────────────────────────────
    def _on_turn(self, pcm):
        self._spawn(self._run_turn, pcm)           # non-blocking: STT/LLM/TTS off the audio path

    def _on_barge_in(self):
        try:
            self.system.cancel_generation()        # sets cancel_flag -> chat_stream halts
        except Exception as e:
            logger.warning(f"[CONV] barge-in cancel_generation failed: {e}")
        sink = self._active_sink
        if sink is not None:
            try:
                sink.stop()                        # cut local audio now
            except Exception as e:
                logger.warning(f"[CONV] barge-in sink.stop failed: {e}")

    # ── the streaming turn ──────────────────────────────────────────────────
    def _run_turn(self, pcm):
        message_id = uuid.uuid4().hex
        try:
            text = self._transcribe_fn(pcm)
            if not (text and text.strip()):
                logger.info("[CONV] turn: no usable speech, skipping")
                return
            logger.info("[CONV] turn: transcribed user utterance -> streaming")

            sink = self._ensure_sink()
            sink.start()
            self._active_sink = sink
            publish(Events.VOICE_TURN_START, {"message_id": message_id})

            stream, sid, chat = self.system.llm_chat.begin_stream()
            try:
                for event in stream.chat_stream(text):
                    et = event.get("type") if isinstance(event, dict) else None
                    if et == "content":
                        publish(Events.VOICE_TURN_CHUNK,
                                {"message_id": message_id, "text": event.get("text", "")})
                    elif et == "tts_chunk":
                        sink.feed_chunk(event)
                    if getattr(stream, "cancel_flag", False):
                        break
            finally:
                self.system.llm_chat.end_stream(sid, chat)

            sink.finish()
            self._wait_sink(sink)                  # stay RESPONDING until audio finishes
            publish(Events.VOICE_TURN_END, {"message_id": message_id})
        except Exception as e:
            logger.error(f"[CONV] streaming turn failed: {e}")
        finally:
            self._active_sink = None
            self.engine.turn_finished()            # no-op if a barge-in already moved us on

    # ── sink ────────────────────────────────────────────────────────────────
    def _ensure_sink(self):
        if self._sink is None:
            if self._sink_factory is not None:
                self._sink = self._sink_factory()
            else:
                from core.tts.pumpkin_chunker import PumpkinChunker
                tts = getattr(self.system, "tts", None)
                self._sink = PumpkinChunker(
                    output_device=getattr(tts, "output_device", None),
                    output_rate=getattr(tts, "output_rate", None) or 48000,
                )
        return self._sink

    def _wait_sink(self, sink, timeout=180):
        w = getattr(sink, "_worker", None)
        if w is not None and hasattr(w, "join"):
            w.join(timeout=timeout)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _spawn(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def _whisper_transcribe(self, pcm):
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
