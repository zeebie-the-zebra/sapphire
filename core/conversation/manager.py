"""Conversation-mode manager (v3 Rollout 2b) — "true speech mode".

Ties the driver + VAD gate + front-door to the fail-safe handoff. `start_local`
enters true speech mode using the local mic (headphone tier); `stop` exits and
restores wakeword. One manager per system; lives on the VoiceChatSystem.

The gate (silero) and source_factory are injectable so this is unit-testable
without loading the VAD model or opening a real mic.
"""
import logging

from core.conversation.driver import ConversationDriver

logger = logging.getLogger(__name__)


class ConversationManager:
    def __init__(self, system, gate=None, source_factory=None):
        self.system = system
        self.driver = ConversationDriver(system)
        self._gate = gate                       # None -> lazy-load silero on first use
        self._source_factory = source_factory or self._default_local_source

    def _ensure_gate(self):
        if self._gate is None:
            from core.conversation.vad import SpeechGate
            self._gate = SpeechGate()
        return self._gate

    def _default_local_source(self, driver, gate):
        from core.conversation.local_source import LocalMicSource
        src = LocalMicSource(driver, gate)
        src.start()                              # raises on failure -> handoff restores wakeword
        return src

    @property
    def active(self):
        return bool(getattr(self.system, "conversation_mode_enabled", False))

    def start_local(self):
        """Enter true speech mode on the local mic. Returns True if active."""
        if self.active:
            return True
        gate = self._ensure_gate()

        def acquire():
            return self._source_factory(self.driver, gate)

        ok = self.system.enter_conversation_mode(acquire)
        logger.info(f"[CONV] start_local -> {'ON' if ok else 'failed (wakeword intact)'}")
        return ok

    def stop(self):
        """Exit true speech mode and restore wakeword (idempotent)."""
        self.system.exit_conversation_mode()
        self.driver.reset()
