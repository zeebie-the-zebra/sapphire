"""Tests for the brain-side streaming TTS pump.

Focus: the orchestration glue around SpeechChunker — that push() yields
the right SSE events, that disabled/no-provider/no-stream-support all
correctly fall back to no-op, and that cancel/close don't leak threads.

Synth is mocked — the chunker itself is tested separately in
tests/test_tts_streaming.py.
"""
import base64
import time
from unittest.mock import MagicMock

import pytest

from core.tts.stream_pump import StreamingTTSPump


class _FakeProvider:
    audio_content_type = "audio/ogg"
    supports_streaming = True

    def __init__(self, audio_bytes=b"OGGS\x00fake", delay=0.0, fail=False):
        self.audio_bytes = audio_bytes
        self.delay = delay
        self.fail = fail
        self.calls = []

    def generate(self, text, voice, speed):
        self.calls.append((text, voice, speed))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("simulated synth failure")
        return self.audio_bytes


def _make_system(provider=None, voice="af_heart", speed=1.0, tts_attr=True):
    sys = MagicMock()
    if tts_attr:
        sys.tts._provider = provider
        sys.tts.voice_name = voice
        sys.tts.speed = speed
    else:
        sys.tts = None
    return sys


@pytest.fixture
def enable_streaming(monkeypatch):
    import config
    monkeypatch.setattr(config, "TTS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TTS_STREAMING_ENABLED", True, raising=False)


def test_disabled_when_streaming_setting_off(monkeypatch):
    import config
    monkeypatch.setattr(config, "TTS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TTS_STREAMING_ENABLED", False, raising=False)
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    assert pump.enabled is False
    assert pump.push("Hello. World. ") == []
    assert list(pump.flush_and_close()) == []


def test_disabled_when_no_provider(enable_streaming):
    pump = StreamingTTSPump(system=_make_system(tts_attr=False))
    assert pump.enabled is False


def test_disabled_when_provider_no_stream_support(enable_streaming):
    class NoStream:
        supports_streaming = False
        def generate(self, *a, **kw): return b""
    pump = StreamingTTSPump(system=_make_system(provider=NoStream()))
    assert pump.enabled is False


def test_first_push_emits_stream_start(enable_streaming):
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    out = pump.push("Just a short bit")  # no boundary yet
    types = [e["type"] for e in out]
    assert "tts_stream_start" in types
    pump.cancel()


def test_complete_sentence_produces_audio_event(enable_streaming):
    fp = _FakeProvider(audio_bytes=b"FAKE_OGG_BYTES")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    # Push enough to satisfy sentence boundary (period + space + capital)
    pump.push("Hello there. ")
    pump.push("More text here.")
    out = list(pump.flush_and_close())
    # Expect at least one tts_chunk and a final tts_stream_end
    types = [e["type"] for e in out]
    assert "tts_stream_end" in types
    chunks = [e for e in out if e["type"] == "tts_chunk"]
    assert len(chunks) >= 1
    # Audio is base64-encoded
    decoded = base64.b64decode(chunks[0]["audio_b64"])
    assert decoded == b"FAKE_OGG_BYTES"
    # Metadata round-trips
    assert chunks[0]["content_type"] == "audio/ogg"
    assert "boundary" in chunks[0]
    assert "pause_after_ms" in chunks[0]


def test_synth_failure_swallowed_no_event(enable_streaming):
    fp = _FakeProvider(fail=True)
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("Hello there. ")
    pump.push("More.")
    out = list(pump.flush_and_close())
    chunks = [e for e in out if e["type"] == "tts_chunk"]
    assert chunks == []  # all synths failed
    assert any(e["type"] == "tts_stream_end" for e in out)


def test_cancel_no_more_events(enable_streaming):
    fp = _FakeProvider(delay=0.2)
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("First sentence here. ")
    pump.push("Second sentence here. ")
    pump.push("Third.")
    # Cancel without waiting
    pump.cancel()
    # Subsequent push should be no-op
    assert pump.push("more") == []
    # flush_and_close after cancel is no-op
    assert list(pump.flush_and_close()) == []


def test_push_with_no_text_no_events(enable_streaming):
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    assert pump.push("") == []
    assert pump.push(None) == []


def test_flush_emits_end_only_when_started(enable_streaming):
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    # No push happened
    out = list(pump.flush_and_close())
    assert out == []  # no stream_start means no stream_end either


# ---------------------------------------------------------------------------
# Hook surface tests (M5)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_hooks():
    """Each hook test starts with a clean hook registry and restores after."""
    from core.hooks import hook_runner
    snapshot = dict(hook_runner._hooks)
    hook_runner._hooks.clear()
    hook_runner._sorted.clear()
    yield hook_runner
    hook_runner._hooks.clear()
    hook_runner._sorted.clear()
    hook_runner._hooks.update(snapshot)


def test_tts_stream_start_hook_fires(enable_streaming, fresh_hooks):
    seen = []
    fresh_hooks.register("tts_stream_start", lambda ev: seen.append(ev.metadata.copy()), plugin_name="t")
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    pump.push("Anything.")
    assert len(seen) == 1
    assert "stream_id" in seen[0]
    assert seen[0]["voice"] == "af_heart"
    pump.cancel()


def test_tts_stream_start_skip_disables_turn(enable_streaming, fresh_hooks):
    def cancel(ev):
        ev.skip_tts = True
    fresh_hooks.register("tts_stream_start", cancel, plugin_name="killer")
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    out = pump.push("Hello there. ")
    out += pump.push("More text here.")
    out += list(pump.flush_and_close())
    # Skip means no events emitted at all (not even stream_start in output)
    assert out == []


def test_tts_chunk_text_hook_can_mutate(enable_streaming, fresh_hooks):
    fp = _FakeProvider()
    def mutate(ev):
        ev.tts_text = (ev.tts_text or "").upper()
    fresh_hooks.register("tts_chunk_text", mutate, plugin_name="up")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("Hello there. ")
    pump.push("More text.")
    list(pump.flush_and_close())
    # Provider should have been called with uppercased text
    assert any(call[0].startswith("HELLO") for call in fp.calls), fp.calls


def test_tts_chunk_text_skip_drops_chunk(enable_streaming, fresh_hooks):
    fp = _FakeProvider()
    def drop_first(ev):
        if ev.metadata.get("chunk_index") == 0:
            ev.skip_tts = True
    fresh_hooks.register("tts_chunk_text", drop_first, plugin_name="dropper")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("First sentence here. ")
    pump.push("Second sentence here. ")
    pump.push("Third.")
    out = list(pump.flush_and_close())
    # First chunk was skipped — provider was NOT called for it
    chunk_texts = [c[0] for c in fp.calls]
    assert not any("First sentence" in t for t in chunk_texts)
    # Other chunks still synthesized
    assert any("Second sentence" in t for t in chunk_texts)


def test_tts_chunk_audio_hook_can_replace_bytes(enable_streaming, fresh_hooks):
    fp = _FakeProvider(audio_bytes=b"ORIGINAL")
    def replace(ev):
        carrier = ev.metadata.get("audio")
        if carrier is not None:
            carrier["audio_bytes"] = b"REPLACED"
    fresh_hooks.register("tts_chunk_audio", replace, plugin_name="swapper")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("Hello there. ")
    pump.push("More.")
    out = list(pump.flush_and_close())
    chunks = [e for e in out if e["type"] == "tts_chunk"]
    assert chunks
    assert base64.b64decode(chunks[0]["audio_b64"]) == b"REPLACED"


def test_tts_chunk_audio_hook_can_drop(enable_streaming, fresh_hooks):
    fp = _FakeProvider(audio_bytes=b"ORIGINAL")
    def drop(ev):
        carrier = ev.metadata.get("audio")
        if carrier is not None:
            carrier["audio_bytes"] = None
    fresh_hooks.register("tts_chunk_audio", drop, plugin_name="muter")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("Hello there. ")
    pump.push("More.")
    out = list(pump.flush_and_close())
    chunks = [e for e in out if e["type"] == "tts_chunk"]
    assert chunks == []  # all chunks muted


def test_tts_stream_end_hook_observes_completion(enable_streaming, fresh_hooks):
    seen = []
    fresh_hooks.register("tts_stream_end", lambda ev: seen.append(ev.metadata.copy()), plugin_name="t")
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    pump.push("Hello there. ")
    pump.push("More.")
    list(pump.flush_and_close())
    assert len(seen) == 1
    assert seen[0]["interrupted"] is False
    assert seen[0]["chunk_count"] >= 1
    assert "stream_id" in seen[0]


def test_tts_stream_end_hook_fires_on_cancel_with_interrupted_true(enable_streaming, fresh_hooks):
    seen = []
    fresh_hooks.register("tts_stream_end", lambda ev: seen.append(ev.metadata.copy()), plugin_name="t")
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider(delay=0.1)))
    pump.push("Hello there. ")
    pump.push("More.")
    pump.cancel()
    assert len(seen) == 1
    assert seen[0]["interrupted"] is True


def test_settings_chunk_bounds_reach_chunker(monkeypatch, enable_streaming):
    """User-tunable min/max chars from settings get applied to the chunker."""
    import config
    monkeypatch.setattr(config, "TTS_STREAMING_MIN_CHARS", 50, raising=False)
    monkeypatch.setattr(config, "TTS_STREAMING_MAX_CHARS", 300, raising=False)
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    assert pump.chunker.min_chars == 50
    assert pump.chunker.max_chars == 300
    pump.cancel()


def test_settings_clamps_protect_against_bad_input(monkeypatch, enable_streaming):
    """Garbage settings (zero, negative, max < min) don't yield a broken chunker."""
    import config
    monkeypatch.setattr(config, "TTS_STREAMING_MIN_CHARS", 0, raising=False)
    monkeypatch.setattr(config, "TTS_STREAMING_MAX_CHARS", -1, raising=False)
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    assert pump.chunker.min_chars >= 5
    assert pump.chunker.max_chars > pump.chunker.min_chars
    pump.cancel()


def test_stream_id_stable_across_hooks(enable_streaming, fresh_hooks):
    """A plugin can correlate the 4 hooks via stream_id within one turn."""
    ids = {}
    fresh_hooks.register("tts_stream_start", lambda ev: ids.setdefault("start", ev.metadata["stream_id"]), plugin_name="t")
    fresh_hooks.register("tts_chunk_text", lambda ev: ids.setdefault("text", ev.metadata["stream_id"]), plugin_name="t")
    fresh_hooks.register("tts_chunk_audio", lambda ev: ids.setdefault("audio", ev.metadata["stream_id"]), plugin_name="t")
    fresh_hooks.register("tts_stream_end", lambda ev: ids.setdefault("end", ev.metadata["stream_id"]), plugin_name="t")
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    pump.push("Hello there. ")
    pump.push("More.")
    list(pump.flush_and_close())
    assert ids["start"] == ids["text"] == ids["audio"] == ids["end"]


# ---------------------------------------------------------------------------
# M7 Stop coordination — cancel_check during flush_and_close
# ---------------------------------------------------------------------------


def test_flush_polls_cancel_check_and_bails_early(enable_streaming, fresh_hooks):
    """User hits Stop AFTER LLM finishes but BEFORE all synth completes —
    flush_and_close must detect the cancel and emit end(interrupted=True)
    instead of blocking until every future resolves."""
    cancel_flag = {"value": False}
    fp = _FakeProvider(delay=0.4)  # slow synth so cancel can race in
    pump = StreamingTTSPump(
        system=_make_system(provider=fp),
        cancel_check=lambda: cancel_flag["value"],
    )
    pump.push("First sentence here. ")
    pump.push("Second sentence here. ")
    pump.push("Third sentence here.")
    # Trip cancel BEFORE flush — flush should bail immediately on first poll
    cancel_flag["value"] = True
    out = list(pump.flush_and_close())
    end_events = [e for e in out if e["type"] == "tts_stream_end"]
    assert len(end_events) == 1
    assert end_events[0]["interrupted"] is True


def test_flush_fires_end_hook_with_interrupted_true_on_user_stop(enable_streaming, fresh_hooks):
    """Plugins listening to tts_stream_end must see interrupted=True so
    they can finalize state properly (e.g. close a partial recording)."""
    seen = []
    fresh_hooks.register("tts_stream_end", lambda ev: seen.append(ev.metadata.copy()), plugin_name="t")
    cancel_flag = {"value": False}
    fp = _FakeProvider(delay=0.4)
    pump = StreamingTTSPump(
        system=_make_system(provider=fp),
        cancel_check=lambda: cancel_flag["value"],
    )
    pump.push("First sentence. ")
    pump.push("Second sentence here.")
    cancel_flag["value"] = True
    list(pump.flush_and_close())
    assert len(seen) == 1
    assert seen[0]["interrupted"] is True


def test_flush_normal_completion_marks_not_interrupted(enable_streaming, fresh_hooks):
    """Sanity: no cancel signal means interrupted=False in both the
    SSE event and the hook payload."""
    seen = []
    fresh_hooks.register("tts_stream_end", lambda ev: seen.append(ev.metadata.copy()), plugin_name="t")
    pump = StreamingTTSPump(
        system=_make_system(provider=_FakeProvider()),
        cancel_check=lambda: False,
    )
    pump.push("Hello there. ")
    pump.push("More.")
    out = list(pump.flush_and_close())
    end_events = [e for e in out if e["type"] == "tts_stream_end"]
    assert end_events[0]["interrupted"] is False
    assert seen[0]["interrupted"] is False


def test_cancel_check_exception_treated_as_false(enable_streaming):
    """A misbehaving cancel_check (raises) must not crash the drain loop —
    treat it as 'not cancelled' and continue."""
    pump = StreamingTTSPump(
        system=_make_system(provider=_FakeProvider()),
        cancel_check=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    pump.push("Hello there. ")
    pump.push("More.")
    out = list(pump.flush_and_close())
    assert any(e["type"] == "tts_stream_end" for e in out)


def test_cancel_method_still_works_after_flush(enable_streaming):
    """The legacy cancel() path (called from finally) must remain a safe
    no-op after flush_and_close has already closed the pump."""
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    pump.push("Hello there. ")
    pump.push("More.")
    list(pump.flush_and_close())
    # Second cancel/close — should not raise or fire end-hook again
    pump.cancel()  # safe no-op


# ---------------------------------------------------------------------------
# Plugin-contract guard (herring-table #4 #8 #12)
# ---------------------------------------------------------------------------


def test_plugin_non_string_tts_text_does_not_crash_turn(enable_streaming, fresh_hooks):
    """A buggy plugin that sets ev.tts_text to a non-string (list/dict/int)
    must NOT crash the LLM turn — pump uses original text and logs warning."""
    fp = _FakeProvider()
    def bad_handler(ev):
        ev.tts_text = ["accidentally", "a", "list"]
    fresh_hooks.register("tts_chunk_text", bad_handler, plugin_name="buggy")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    # No exception should escape push() or flush_and_close()
    pump.push("Hello there. ")
    pump.push("More text here.")
    out = list(pump.flush_and_close())
    # Synth was called with ORIGINAL text (not the bad list), so chunks emitted
    chunks = [e for e in out if e["type"] == "tts_chunk"]
    assert chunks, "expected chunks despite buggy plugin"
    # First call should have used the original text, not the list
    assert any("Hello" in call[0] for call in fp.calls), fp.calls


def test_plugin_empty_string_tts_text_skips_chunk_not_clobbers_to_original(enable_streaming, fresh_hooks):
    """Plugin setting ev.tts_text = '' means 'mute this chunk', NOT 'use
    original'. Content-moderation plugins rely on this contract."""
    fp = _FakeProvider()
    def mute(ev):
        if ev.metadata.get("chunk_index") == 0:
            ev.tts_text = ""
    fresh_hooks.register("tts_chunk_text", mute, plugin_name="muter")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("First sentence here. ")
    pump.push("Second sentence here. ")
    pump.push("Third.")
    list(pump.flush_and_close())
    # First chunk MUST NOT have been synthesized — its text was muted to ""
    synth_texts = [call[0] for call in fp.calls]
    assert not any("First sentence" in t for t in synth_texts), (
        f"plugin muted first chunk but original text was synthesized anyway: {synth_texts}"
    )


def test_plugin_whitespace_tts_text_also_skips_chunk(enable_streaming, fresh_hooks):
    """Whitespace-only counts as 'mute', not 'use original'."""
    fp = _FakeProvider()
    def whitespace(ev):
        ev.tts_text = "   \t\n  "
    fresh_hooks.register("tts_chunk_text", whitespace, plugin_name="wsp")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("Anything. ")
    pump.push("More text here.")
    list(pump.flush_and_close())
    # No synth should have happened at all — every chunk was whitespace-muted
    assert fp.calls == [], f"whitespace-muted chunks were synthesized: {fp.calls}"


def test_plugin_None_tts_text_uses_original(enable_streaming, fresh_hooks):
    """ev.tts_text=None (plugin didn't touch it) means 'use original text'."""
    fp = _FakeProvider()
    def noop(ev):
        # Don't touch tts_text; HookEvent initializes it from the kwarg passed
        # by _fire_hook, but None at the field-default level means "untouched"
        pass
    fresh_hooks.register("tts_chunk_text", noop, plugin_name="noop")
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    pump.push("Hello there. ")
    pump.push("More.")
    list(pump.flush_and_close())
    # Synth should have used original chunk text
    assert any("Hello" in call[0] for call in fp.calls), fp.calls


class _StreamingFakeProvider:
    """Like _FakeProvider but with a real generate_stream that yields
    multiple segments per chunk — exercises the new pump wiring (herring #7)."""
    audio_content_type = "audio/ogg"
    supports_streaming = True

    def __init__(self, segments_per_chunk=3, segment_bytes=b"OGGS_FAKE_SEGMENT"):
        self.segments_per_chunk = segments_per_chunk
        self.segment_bytes = segment_bytes
        self.calls = []

    def generate(self, text, voice, speed):
        # Should NOT be called when supports_streaming is True
        raise AssertionError("generate() should not be called when streaming")

    def generate_stream(self, text, voice, speed):
        self.calls.append((text, voice, speed))
        for i in range(self.segments_per_chunk):
            yield self.segment_bytes + bytes([i])


def test_streaming_provider_emits_one_event_per_segment(enable_streaming):
    """When provider.supports_streaming=True, the pump should emit one
    tts_chunk SSE event per yielded segment — that's the M2 latency win
    we wired up 2026-05-18 (herring #7). Before this wiring, all segments
    were collected into one blob and one event was emitted per chunk.

    Note: push() can emit segments inline (when worker finished by the
    time the next push runs) — test collects events from ALL three
    method calls so we don't miss inline drains."""
    fp = _StreamingFakeProvider(segments_per_chunk=4)
    pump = StreamingTTSPump(system=_make_system(provider=fp))
    out = []
    out.extend(pump.push("Hello there. "))
    out.extend(pump.push("More text here."))
    out.extend(list(pump.flush_and_close()))
    chunks = [e for e in out if e["type"] == "tts_chunk"]
    # 2 pump-chunks × 4 segments each = 8 events
    assert len(chunks) == 8, f"expected 8 segment events, got {len(chunks)}: {[c['index'] for c in chunks]}"
    # First 4 should be index 0 (from first pump-chunk), next 4 index 1
    indices = [c["index"] for c in chunks]
    assert indices == [0, 0, 0, 0, 1, 1, 1, 1], f"order/index wrong: {indices}"
    # The legacy generate() must NOT have been called
    assert len(fp.calls) == 2  # one generate_stream call per pump-chunk


def test_streaming_provider_preserves_order_with_concurrent_workers(enable_streaming):
    """With 2 executor workers, the FIRST pump-chunk's segments must emit
    before the SECOND pump-chunk's even if the second's worker finishes
    first (faster synth). Order is preserved at the deque level."""
    import time as _time

    class _OrderedProvider:
        audio_content_type = "audio/ogg"
        supports_streaming = True
        def __init__(self):
            self.calls = []
        def generate(self, *a, **kw):
            raise AssertionError("should not call generate")
        def generate_stream(self, text, voice, speed):
            self.calls.append(text)
            # Text-keyed delay so "first" pump-chunk is slow regardless
            # of which executor worker picks up which future.
            if "First" in text:
                _time.sleep(0.15)
            label = text.split()[0]  # 'First.' or 'Second'
            for i in range(2):
                yield f"{label}_seg_{i}".encode()

    op = _OrderedProvider()
    pump = StreamingTTSPump(system=_make_system(provider=op))
    pump.push("First chunk. ")
    pump.push("Second chunk text.")
    out = list(pump.flush_and_close())
    chunks = [e for e in out if e["type"] == "tts_chunk"]
    decoded = [base64.b64decode(c["audio_b64"]).decode() for c in chunks]
    # First pump-chunk's segments (despite slower synth) emit BEFORE
    # second's. The deque holds the order; workers don't.
    assert decoded == [
        "First_seg_0", "First_seg_1",
        "Second_seg_0", "Second_seg_1",
    ], f"order broken: {decoded}"


def test_skip_tts_at_stream_start_fires_end_hook_for_plugin_cleanup(enable_streaming, fresh_hooks):
    """When a plugin cancels the whole turn via tts_stream_start skip_tts,
    tts_stream_end MUST still fire (with interrupted=True) so plugins that
    opened state in start can finalize — otherwise state leaks across turns."""
    starts = []
    ends = []
    fresh_hooks.register("tts_stream_start", lambda ev: (starts.append(ev.metadata.copy()), setattr(ev, "skip_tts", True)), plugin_name="cancel")
    fresh_hooks.register("tts_stream_end", lambda ev: ends.append(ev.metadata.copy()), plugin_name="t")
    pump = StreamingTTSPump(system=_make_system(provider=_FakeProvider()))
    out = pump.push("Hello there. ")
    out += list(pump.flush_and_close())
    # No SSE events emitted (plugin cancelled the whole turn)
    assert out == []
    # Start hook fired exactly once
    assert len(starts) == 1
    # End hook ALSO fired with interrupted=True — closing the contract
    assert len(ends) == 1, f"expected end hook to fire on skip-turn cancel, got {ends}"
    assert ends[0]["interrupted"] is True
    assert ends[0]["chunk_count"] == 0
    assert starts[0]["stream_id"] == ends[0]["stream_id"]
