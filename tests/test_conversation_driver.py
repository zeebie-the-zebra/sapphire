"""Conversation turn-driver tests (v3 Rollout 2b).

Bridges the engine to the proven pipeline. We inject a fake transcribe + a mock
system and run turn dispatch synchronously (override _spawn), so we assert the
real wiring without whisper/LLM/audio: a completed utterance drives
process_llm_query with the transcript and returns the engine to IDLE; a barge-in
fires cancel_generation + tts.stop.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.conversation.driver import ConversationDriver
from core.conversation.engine import IDLE, RESPONDING, USER_SPEAKING

SR = 16000


def frame(ms, speech):
    samples = int(SR * ms / 1000)
    return (b"\x00\x00" * samples, speech)


def _driver(transcript="hello sapphire", **engine_kw):
    system = MagicMock()
    kw = dict(endpoint_silence_ms=300, min_speech_ms=100, barge_hold_ms=90)
    kw.update(engine_kw)
    d = ConversationDriver(system, transcribe_fn=lambda pcm: transcript, **kw)
    # Run turn dispatch synchronously for deterministic tests.
    d._spawn = lambda target, *args: target(*args)
    return d, system


def test_completed_utterance_drives_llm_and_returns_idle():
    d, system = _driver(transcript="what time is it")
    d.push_frame(*frame(150, True))           # speech
    for _ in range(3):                         # 300ms silence -> endpoint -> turn
        d.push_frame(*frame(100, False))
    system.process_llm_query.assert_called_once_with("what time is it")
    system.tts.wait.assert_called()            # waited for playback
    assert d.engine.state == IDLE              # turn_finished returned us to idle


def test_empty_transcript_skips_llm_but_finishes():
    d, system = _driver(transcript="")          # whisper heard nothing usable
    d.push_frame(*frame(150, True))
    for _ in range(3):
        d.push_frame(*frame(100, False))
    system.process_llm_query.assert_not_called()
    assert d.engine.state == IDLE


def test_barge_in_cancels_generation_and_tts():
    # Don't auto-run the turn thread here: we want to sit in RESPONDING and barge.
    system = MagicMock()
    d = ConversationDriver(system, transcribe_fn=lambda pcm: "long answer",
                           endpoint_silence_ms=300, min_speech_ms=100, barge_hold_ms=90)
    d._spawn = lambda target, *args: None      # don't actually run the turn
    d.push_frame(*frame(150, True))
    for _ in range(3):
        d.push_frame(*frame(100, False))        # -> RESPONDING (turn dispatch suppressed)
    assert d.engine.state == RESPONDING
    d.push_frame(*frame(100, True))             # barge over the response
    system.cancel_generation.assert_called_once()
    system.tts.stop.assert_called_once()
    assert d.engine.state == USER_SPEAKING      # now capturing the barge-in utterance


def test_turn_finished_is_noop_after_barge():
    # If a barge already transitioned us, the stale turn thread's turn_finished
    # must not clobber the new utterance.
    d, system = _driver(transcript="answer")
    d._spawn = lambda target, *args: None
    d.push_frame(*frame(150, True))
    for _ in range(3):
        d.push_frame(*frame(100, False))
    assert d.engine.state == RESPONDING
    d.push_frame(*frame(100, True))             # barge -> USER_SPEAKING
    d.engine.turn_finished()                    # stale finish from the cancelled turn
    assert d.engine.state == USER_SPEAKING      # unchanged — guard held
