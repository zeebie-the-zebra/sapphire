"""Conversation manager + VAD gate tests (v3 Rollout 2b).

Gate + source are injected, so no silero model load and no real mic — we assert
the manager wires the front-door through the fail-safe handoff and exits cleanly.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.conversation.manager import ConversationManager
from core.conversation.vad import SpeechGate


# ── SpeechGate ──────────────────────────────────────────────────────────────
def test_speech_gate_threshold():
    probs = iter([0.9, 0.1, 0.6, 0.49])
    g = SpeechGate(threshold=0.5, score_fn=lambda chunk: next(probs))
    assert g.is_speech(b"x") is True       # 0.9 >= 0.5
    assert g.is_speech(b"x") is False      # 0.1
    assert g.is_speech(b"x") is True       # 0.6
    assert g.is_speech(b"x") is False      # 0.49 < 0.5


def test_speech_gate_swallows_scorer_error():
    def boom(chunk):
        raise RuntimeError("vad blew up")
    g = SpeechGate(threshold=0.5, score_fn=boom)
    assert g.is_speech(b"x") is False      # degrades to "not speech", never raises


# ── ConversationManager ─────────────────────────────────────────────────────
def _system_with_real_handoff():
    """Mock system whose enter/exit run the acquire/close like the real handoff,
    so we can prove the manager drives them end to end."""
    system = MagicMock()
    system.conversation_mode_enabled = False

    def enter(acquire):
        try:
            sess = acquire()
        except Exception:
            return False
        if sess is None:
            return False
        system.conversation_session = sess
        system.conversation_mode_enabled = True
        return True
    system.enter_conversation_mode.side_effect = enter

    def exit_():
        sess = getattr(system, "conversation_session", None)
        if sess is not None and hasattr(sess, "close"):
            sess.close()
        system.conversation_session = None
        system.conversation_mode_enabled = False
    system.exit_conversation_mode.side_effect = exit_
    return system


def test_manager_start_local_enters_via_handoff():
    system = _system_with_real_handoff()
    source = MagicMock()
    made = []
    mgr = ConversationManager(system, gate=MagicMock(),
                              source_factory=lambda d, g: (made.append((d, g)) or source))
    assert mgr.start_local() is True
    assert mgr.active is True
    assert len(made) == 1                  # the front-door source was built
    assert made[0][0] is mgr.driver        # driver handed to the source


def test_manager_start_when_active_is_noop():
    system = _system_with_real_handoff()
    system.conversation_mode_enabled = True
    calls = []
    mgr = ConversationManager(system, gate=MagicMock(),
                              source_factory=lambda d, g: calls.append(1))
    assert mgr.start_local() is True
    assert calls == []                     # didn't re-acquire


def test_manager_stop_exits_and_closes_source():
    system = _system_with_real_handoff()
    source = MagicMock()
    mgr = ConversationManager(system, gate=MagicMock(), source_factory=lambda d, g: source)
    mgr.start_local()
    mgr.stop()
    assert mgr.active is False
    source.close.assert_called_once()      # source torn down on exit


def test_manager_source_failure_keeps_wakeword():
    """If the front-door raises, the handoff returns False and mode stays off."""
    system = _system_with_real_handoff()

    def boom(d, g):
        raise RuntimeError("mic busy / WASAPI")
    mgr = ConversationManager(system, gate=MagicMock(), source_factory=boom)
    assert mgr.start_local() is False
    assert mgr.active is False
