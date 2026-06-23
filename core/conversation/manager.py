"""Conversation-mode manager (v3 Rollout 2b) — "true speech mode".

Ties the driver + VAD gate + front-door to the fail-safe handoff. `start_local`
enters true speech mode using the local mic (headphone tier); `stop` exits and
restores wakeword. One manager per system; lives on the VoiceChatSystem.

The engine/gate tunables (VAD threshold, barge-hold, min-speech, endpoint-silence)
are read FRESH from settings on each `start_local`, so changing them in
Settings > Conversation takes effect on the next activation — no restart. The gate
and source_factory are injectable so this is unit-testable without silero or a mic.
"""
import logging

from core.conversation.driver import ConversationDriver

logger = logging.getLogger(__name__)


class ConversationManager:
    def __init__(self, system, gate=None, source_factory=None):
        self.system = system
        self._injected_gate = gate                 # tests inject; prod builds fresh per start
        self.driver = None                         # built fresh per start_local with tunables
        self._source_factory = source_factory or self._default_local_source

    def _default_local_source(self, driver, gate):
        import config
        tier = str(getattr(config, "CONVERSATION_AUDIO_TIER", "duplex")).lower()
        if tier == "headphone":
            from core.conversation.local_source import LocalMicSource
            src = LocalMicSource(driver, gate)
            src.start()                            # raises on failure -> handoff restores wakeword
            return src
        # duplex tier (default): one sd.Stream doing mic-in + TTS-out, DTLN cancels her echo
        # so she doesn't barge-in on herself through open speakers.
        from core.conversation.duplex_source import DuplexConversationSource
        model = str(getattr(config, "CONVERSATION_DTLN_MODEL", "256"))
        delay = float(getattr(config, "CONVERSATION_AEC_DELAY_MS", 0))     # 0 off; <0 auto; >0 manual ms
        guard = float(getattr(config, "CONVERSATION_BARGE_GUARD_MS", 300))
        floor = float(getattr(config, "CONVERSATION_BARGE_RMS_FLOOR", 0.03))
        src = DuplexConversationSource(driver, gate, dtln_model=model, aec_delay_ms=delay,
                                       barge_guard_ms=guard, barge_rms_floor=floor)
        src.start()                                # opens duplex stream; raises -> handoff restores wakeword
        driver.set_sink(src)                       # the SAME object is the TTS sink
        return src

    def _build_gate(self):
        if self._injected_gate is not None:
            return self._injected_gate
        import config
        from core.conversation.vad import SpeechGate
        return SpeechGate(threshold=float(getattr(config, "CONVERSATION_VAD_THRESHOLD", 0.5)))

    @property
    def active(self):
        return bool(getattr(self.system, "conversation_mode_enabled", False))

    def start_local(self):
        """Enter true speech mode on the local mic. Returns True if active."""
        if self.active:
            return True
        import config
        # Rebuild driver + gate from current settings so tuning applies without restart.
        self.driver = ConversationDriver(
            self.system,
            endpoint_silence_ms=int(getattr(config, "CONVERSATION_ENDPOINT_SILENCE_MS", 700)),
            min_speech_ms=int(getattr(config, "CONVERSATION_MIN_SPEECH_MS", 200)),
            barge_hold_ms=int(getattr(config, "CONVERSATION_BARGE_HOLD_MS", 90)),
        )
        gate = self._build_gate()

        def acquire():
            return self._source_factory(self.driver, gate)

        ok = self.system.enter_conversation_mode(acquire)
        logger.info(f"[CONV] start_local -> {'ON' if ok else 'failed (wakeword intact)'}")
        return ok

    def stop(self):
        """Exit true speech mode and restore wakeword (idempotent)."""
        self.system.exit_conversation_mode()
        if self.driver is not None:
            self.driver.reset()
