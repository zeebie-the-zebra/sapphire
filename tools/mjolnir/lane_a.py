"""Lane A — headless mocked-Audio scenarios.

Spawns a tiny HTTP server from the project root, navigates Playwright/Chromium
to the test page, replaces window.Audio with MjolnirAudio (the instrumented
mock), and runs each scenario's chunk-arrival sequence + user-actions while
recording lifecycle events. Then validates universal + scenario-specific
invariants against the recorded events.

No real Sapphire backend required. Fast feedback for state-machine bugs.
"""
import asyncio
import dataclasses
import http.server
import socketserver
import threading
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

from . import scenarios

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _QuietHTTPServer(socketserver.TCPServer):
    """Subclass to suppress request-log spam and allow address reuse."""
    allow_reuse_address = True


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args, **_kwargs):
        pass  # silence per-request logs


def _start_static_server(port: int = 0) -> tuple:
    """Start a tiny HTTP server serving the project root in a daemon thread.
    Returns (port, shutdown_fn)."""
    handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(PROJECT_ROOT), **kwargs)
    server = _QuietHTTPServer(("127.0.0.1", port), handler)
    chosen_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()

    return chosen_port, shutdown


async def _run_one_scenario(page, scenario: scenarios.Scenario) -> dict:
    """Execute one scenario in the loaded page. Returns invariant results."""
    # Push the mock config + reset observer state
    mock_cfg = dataclasses.asdict(scenario.mock_config)
    # JS uses camelCase
    js_cfg = {
        "playLatencyMs": mock_cfg["play_latency_ms"],
        "audioDurationMs": mock_cfg["audio_duration_ms"],
        "autoplayBlocked": mock_cfg["autoplay_blocked"],
        "playRejectsWith": mock_cfg["play_rejects_with"],
        "onendedNeverFires": mock_cfg["onended_never_fires"],
        "onerrorAfterMs": mock_cfg["onerror_after_ms"],
    }
    await page.evaluate("(cfg) => window.MjolnirObserver.reset(cfg)", js_cfg)

    stream_id = f"mjolnir-{scenario.name}"

    # Dispatch tts_stream_start
    await page.evaluate(
        """(data) => window._busDispatch('tts_stream_start', data)""",
        {"type": "tts_stream_start", "stream_id": stream_id},
    )

    # Schedule chunks + user_actions on a single timeline
    timeline = []
    elapsed = 0
    for chunk in scenario.chunks:
        elapsed += chunk.delay_ms
        timeline.append({"at": elapsed, "kind": "chunk", "data": dataclasses.asdict(chunk)})
    for action in scenario.user_actions:
        timeline.append({"at": action.delay_ms, "kind": "action", "data": dataclasses.asdict(action)})
    timeline.sort(key=lambda e: e["at"])

    # Walk timeline — sleep deltas, dispatch
    last = 0
    for entry in timeline:
        delta = entry["at"] - last
        if delta > 0:
            await asyncio.sleep(delta / 1000.0)
        last = entry["at"]

        if entry["kind"] == "chunk":
            chunk = entry["data"]
            # NOTE: bus event name is 'tts_stream_chunk' (set by api.js when
            # consuming the SSE 'tts_chunk' wire message). audio.js subscribes
            # via that bus name — not the wire name.
            # After dispatch, the just-created MjolnirAudio is the last in
            # the observer's instance list; stamp the chunk index on it for
            # later invariant checks.
            await page.evaluate(
                """(args) => {
                    const beforeCount = window.MjolnirObserver.instances.length;
                    window._busDispatch('tts_stream_chunk', {
                        type: 'tts_chunk',
                        audio_b64: args.audio_b64,
                        content_type: 'audio/ogg',
                        index: args.index,
                        boundary: args.boundary,
                        pause_after_ms: args.pause_after_ms,
                        stream_id: args.stream_id,
                        text: args.text,
                    });
                    const after = window.MjolnirObserver.instances.length;
                    if (after > beforeCount) {
                        window.MjolnirObserver.instances[after - 1]._chunkIndex = args.index;
                    }
                }""",
                {**chunk, "stream_id": stream_id},
            )
        else:  # user action
            action = entry["data"]["action"]
            if action == "stop":
                await page.evaluate("() => window._audio.stop(true)")
            elif action == "replay":
                # Simulate new stream — bump stream_id and dispatch a new start
                await page.evaluate(
                    """(sid) => window._busDispatch('tts_stream_start', {
                        type: 'tts_stream_start', stream_id: sid
                    })""",
                    f"{stream_id}-replay-{entry['at']}",
                )
            elif action == "newstream":
                await page.evaluate(
                    """(sid) => window._busDispatch('tts_stream_start', {
                        type: 'tts_stream_start', stream_id: sid
                    })""",
                    f"{stream_id}-new-{entry['at']}",
                )

    # Wait for any pending playback to settle. Worst case: every chunk plays
    # sequentially with full latency + duration. Pad +500ms for cleanup.
    per_chunk = (scenario.mock_config.play_latency_ms
                 + scenario.mock_config.audio_duration_ms)
    settle = max(500, per_chunk * max(len(scenario.chunks), 1) + 500)
    await asyncio.sleep(settle / 1000.0)

    # Dispatch stream_end if no user action terminated the stream
    await page.evaluate(
        """(data) => window._busDispatch('tts_stream_end', data)""",
        {
            "type": "tts_stream_end",
            "stream_id": stream_id,
            "chunk_count": len(scenario.chunks),
            "interrupted": False,
        },
    )

    # Brief settle for end handlers
    await asyncio.sleep(0.2)

    # Pull snapshot
    snapshot = await page.evaluate("() => window.MjolnirObserver.snapshot()")
    return snapshot


def _check_invariants(scenario: scenarios.Scenario, snapshot: dict) -> dict:
    """Apply universal + scenario-specific invariants. Return dict of
    {invariant_name: (passed, message)}."""
    results = {}

    # Invariant 1: at most one playing at any time (unless scenario expects overlap)
    max_concurrent = snapshot["maxConcurrentPlaying"]
    if scenario.expects_overlap:
        results["concurrent_playback"] = (True, f"overlap expected, max={max_concurrent}")
    else:
        ok = max_concurrent <= 1
        results["concurrent_playback"] = (
            ok,
            f"max concurrent playing = {max_concurrent} (≤1 expected)",
        )

    # Invariant 2: index ordering (no out-of-order plays). Filter out None
    # entries — those are audio elements created outside our dispatch path
    # (or via paths that didn't stamp an index, like preempt cleanup).
    played = [x for x in snapshot["playedIndexes"] if x is not None]
    is_sorted = all(played[i] <= played[i + 1] for i in range(len(played) - 1))
    if scenario.expects_order_break:
        results["order_preserved"] = (True, f"order break expected, indexes={played}")
    else:
        results["order_preserved"] = (
            is_sorted,
            f"played indexes monotonic non-decreasing: {played}",
        )

    # Invariant 3: no AbortError cascade (more than half of chunks aborting)
    aborts = snapshot["abortErrorCount"]
    attempts = len(snapshot["attemptedIndexes"])
    if scenario.mock_config.play_rejects_with == "AbortError":
        # Scenario INTENTIONALLY rejects everything — pass
        results["no_abort_cascade"] = (
            True,
            f"abort cascade expected ({aborts}/{attempts} aborts)",
        )
    elif scenario.user_actions:
        # User stop/replay legitimately produces AbortErrors — soft check
        results["no_abort_cascade"] = (
            True,
            f"user actions present, {aborts}/{attempts} aborts (informational)",
        )
    else:
        # No user actions and no intentional rejection → aborts should be 0
        ok = aborts == 0
        results["no_abort_cascade"] = (
            ok,
            f"abort error count = {aborts} (0 expected for clean run)",
        )

    return results


async def run_lane_a(only: Optional[str] = None) -> list:
    """Run all Lane A scenarios. Returns list of result dicts."""
    port, shutdown_server = _start_static_server()
    page_url = f"http://127.0.0.1:{port}/tools/mjolnir/page.html"

    results = []
    mock_path = Path(__file__).parent / "audio_mock.js"
    mock_src = mock_path.read_text()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                # Inject the mock BEFORE any document script runs so audio.js's
                # module sees MjolnirAudio when it does `new Audio(url)`.
                await context.add_init_script(mock_src)
                page = await context.new_page()

                # Capture console for debugging
                page.on("pageerror", lambda exc: print(f"  [page error] {exc}"))

                await page.goto(page_url, wait_until="networkidle")
                # Wait for _mjolnirReady flag
                await page.wait_for_function("() => window._mjolnirReady === true",
                                              timeout=10_000)

                for scenario in scenarios.scenarios_for_lane("a"):
                    if only and scenario.name != only:
                        continue
                    print(f"  ► [{scenario.name}] {scenario.description}")
                    try:
                        snapshot = await _run_one_scenario(page, scenario)
                        invariants = _check_invariants(scenario, snapshot)
                    except Exception as e:
                        results.append({
                            "scenario": scenario.name,
                            "lane": "a",
                            "ok": False,
                            "error": f"{type(e).__name__}: {e}",
                            "invariants": {},
                        })
                        print(f"    ✗ exception: {type(e).__name__}: {e}")
                        continue

                    all_pass = all(passed for passed, _ in invariants.values())
                    results.append({
                        "scenario": scenario.name,
                        "lane": "a",
                        "ok": all_pass,
                        "invariants": {k: {"ok": v[0], "msg": v[1]}
                                       for k, v in invariants.items()},
                        "max_concurrent": snapshot["maxConcurrentPlaying"],
                        "instances": snapshot["instanceCount"],
                        "attempted": snapshot["attemptedIndexes"],
                        "played": snapshot["playedIndexes"],
                        "aborts": snapshot["abortErrorCount"],
                    })
                    for name, (passed, msg) in invariants.items():
                        sym = "✓" if passed else "✗"
                        print(f"    {sym} {name}: {msg}")
            finally:
                await browser.close()
    finally:
        shutdown_server()

    return results


if __name__ == "__main__":
    asyncio.run(run_lane_a())
