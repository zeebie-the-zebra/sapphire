"""Conversation streaming-turn driver tests (v3 Rollout 2b).

The driver drives chat_stream: content -> event bus, tts_chunk -> sink. We inject a
fake transcribe, a fake stream (scripted events), and a fake sink, and run turn
dispatch synchronously, so we assert the real wiring without whisper/LLM/audio.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.conversation.driver import ConversationDriver
from core.conversation.engine import IDLE, RESPONDING, USER_SPEAKING
from core.event_bus import Events

SR = 16000


def frame(ms, speech):
    samples = int(SR * ms / 1000)
    return (b"\x00\x00" * samples, speech)


def _driver(transcript="hello sapphire", events=None, sink=None):
    system = MagicMock()
    if events is None:
        events = [
            {"type": "content", "text": "Hi "},
            {"type": "content", "text": "there"},
            {"type": "tts_chunk", "audio_b64": "x", "stream_id": "s"},
            {"type": "done"},
        ]
    fake_stream = MagicMock()
    fake_stream.cancel_flag = False
    fake_stream.chat_stream.return_value = iter(events)
    system.llm_chat.begin_stream.return_value = (fake_stream, "sid", "chat")
    if sink is None:
        sink = MagicMock()
        sink._worker = None
    d = ConversationDriver(system, transcribe_fn=lambda p: transcript,
                           sink_factory=lambda: sink,
                           endpoint_silence_ms=300, min_speech_ms=100, barge_hold_ms=90)
    d._spawn = lambda target, *a: target(*a)   # run the turn synchronously
    return d, system, fake_stream, sink


@patch("core.conversation.driver.publish")
def test_streaming_turn_routes_content_and_tts_then_idle(pub):
    d, system, fs, sink = _driver()
    d.push_frame(*frame(150, True))
    for _ in range(3):
        d.push_frame(*frame(100, False))            # endpoint -> streaming turn

    system.llm_chat.begin_stream.assert_called_once()
    sink.start.assert_called_once()
    sink.feed_chunk.assert_called_once()             # the single tts_chunk
    sink.finish.assert_called_once()
    system.llm_chat.end_stream.assert_called_once()
    assert d.engine.state == IDLE

    # both content events were routed to the bus as VOICE_TURN_CHUNK
    chunk_pubs = [c for c in pub.call_args_list
                  if c.args and c.args[0] == Events.VOICE_TURN_CHUNK]
    assert len(chunk_pubs) == 2
    assert chunk_pubs[0].args[1]["text"] == "Hi "


def test_empty_transcript_skips_stream():
    d, system, fs, sink = _driver(transcript="")
    d.push_frame(*frame(150, True))
    for _ in range(3):
        d.push_frame(*frame(100, False))
    system.llm_chat.begin_stream.assert_not_called()
    sink.start.assert_not_called()
    assert d.engine.state == IDLE


def test_barge_in_cancels_generation_and_stops_sink():
    system = MagicMock()
    sink = MagicMock()
    sink._worker = None
    d = ConversationDriver(system, transcribe_fn=lambda p: "x", sink_factory=lambda: sink,
                           endpoint_silence_ms=300, min_speech_ms=100, barge_hold_ms=90)
    d._spawn = lambda target, *a: None               # suppress the turn thread
    d.push_frame(*frame(150, True))
    for _ in range(3):
        d.push_frame(*frame(100, False))             # -> RESPONDING
    assert d.engine.state == RESPONDING
    d._active_sink = sink                            # simulate a turn-in-progress sink

    d.push_frame(*frame(100, True))                  # barge over the response
    system.cancel_generation.assert_called_once()
    sink.stop.assert_called_once()
    assert d.engine.state == USER_SPEAKING


def test_turn_finished_noop_after_barge():
    d, system, fs, sink = _driver()
    d._spawn = lambda target, *a: None
    d.push_frame(*frame(150, True))
    for _ in range(3):
        d.push_frame(*frame(100, False))
    assert d.engine.state == RESPONDING
    d.push_frame(*frame(100, True))                  # barge -> USER_SPEAKING
    d.engine.turn_finished()                         # stale finish from the cancelled turn
    assert d.engine.state == USER_SPEAKING
