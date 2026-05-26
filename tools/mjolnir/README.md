# Mjolnir — TTS Streaming Hammer

Multi-lane stress harness for Sapphire's streaming TTS pipeline. Built to
catch the bug-classes that surfaced in the May 2026 user reports:
zero-audio AbortError cascades, 10× overlapping playback on slower systems,
and the silent skips that the diagnostic blind spot used to hide.

## Three lanes

| Lane | What | Speed-sim mechanism |
| ---- | ---- | ------------------- |
| **A** | Headless Playwright + mocked `Audio` constructor. Tests the JS state machine in isolation with full control over play() latency, onended timing, autoplay outcomes. | `MockConfig.play_latency_ms`, `audio_duration_ms`, `autoplay_blocked`, `play_rejects_with`, `onended_never_fires`, `onerror_after_ms` |
| **B** | Playwright with **real** Audio playback. Chromium/Brave/Firefox. Uses a tiny silent OGG/Opus blob so real decode paths exercise. CDP CPU throttling per scenario. | `CdpThrottle.cpu_rate` (Chromium-family), `network_profile` |
| **C** | asyncio + httpx. Concurrent POSTs to `/api/tts/stream`. Tests the brain-side pump under load: drop accounting, notice emission, no 5xx under stress. | concurrency count, payload variants (CJK, emoji, empty, long) |

## Setup

```bash
# One-time
conda create -n mjolnir python=3.11 -y
conda activate mjolnir
pip install playwright httpx rich
playwright install chromium firefox

# Lane C requires a Sapphire API token:
#   Sapphire UI → Settings → System → API Keys → + Add 'mjolnir'
export MJOLNIR_API_TOKEN=sk_xxxxxxxxxxxxxxxx

# Optional overrides
export BRAVE_PATH=/usr/bin/brave-browser   # if Brave installed in a non-standard location
export SAPPHIRE_URL=http://localhost:8073  # default
```

## Run

```bash
# Activate the env
conda activate mjolnir

# Default: all lanes
python -m tools.mjolnir.run

# One lane
python -m tools.mjolnir.run --lane a
python -m tools.mjolnir.run --lane b
python -m tools.mjolnir.run --lane c

# One scenario across applicable lanes
python -m tools.mjolnir.run --scenario uniform-fast

# Lane B with a specific browser subset
python -m tools.mjolnir.run --lane b --browsers chromium,firefox
```

Results print to terminal AND are saved as JSON to `tools/mjolnir/output/`
(gitignored).

## Adding scenarios

Edit `scenarios.py`. Each scenario is one entry in `SCENARIOS`:

```python
Scenario(
    name="my-new-scenario",
    description="What this tests",
    chunks=[Chunk(delay_ms=100, text="hi", index=0)],
    mock_config=MockConfig(play_latency_ms=200, audio_duration_ms=500),
    user_actions=[UserAction(delay_ms=350, action="stop")],
    cdp_throttle=CdpThrottle(cpu_rate=4.0),       # Lane B only
    lanes={"a", "b"},                              # subset of {"a","b","c"}
)
```

The three universal invariants are checked automatically:
1. **CONCURRENT_PLAYBACK** — at most one Audio in `paused===false` at any T
2. **ORDER_PRESERVED** — chunks play in index order
3. **NO_ABORT_CASCADE** — abort errors only when expected

## Files

- `scenarios.py` — shared spec (Chunk / UserAction / MockConfig / Scenario dataclasses + SCENARIOS list)
- `page.html` — minimal test page with DOM stubs that audio.js's deps need
- `audio_mock.js` — Lane A: replaces `window.Audio` with `MjolnirAudio` (full control)
- `audio_real_observer.js` — Lane B: wraps real Audio with lifecycle observers
- `silent.ogg` — 300ms silent Opus blob (Lane B audio payload)
- `lane_a.py`, `lane_b.py`, `lane_c.py` — the three execution lanes
- `run.py` — orchestrator + reporter
- `output/` — JSON results (gitignored)
