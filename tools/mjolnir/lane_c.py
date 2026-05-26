"""Lane C — real-backend hammer.

Concurrent POSTs to Sapphire's /api/tts/stream endpoint. Verifies the brain-side
pump under realistic load:
  - No 5xx errors
  - Proper SSE structure (tts_stream_start → N×tts_chunk → tts_stream_end)
  - Drop accounting (notice fires when expected)
  - Server stays responsive across N concurrent streams

Requires:
  - Sapphire running on localhost:8073
  - MJOLNIR_API_TOKEN env var set to a Bearer token from
    Settings → System → API Keys
"""
import asyncio
import json
import os
from typing import Optional

import httpx

from . import scenarios

SAPPHIRE_URL = os.environ.get("SAPPHIRE_URL", "https://localhost:8073")
TOKEN = os.environ.get("MJOLNIR_API_TOKEN", "")
# Sapphire's default cert is self-signed (WEB_UI_SSL_ADHOC=true), so we
# disable verification for localhost. Override via MJOLNIR_VERIFY_SSL=1.
VERIFY_SSL = os.environ.get("MJOLNIR_VERIFY_SSL", "0") == "1"


def _auth_headers() -> dict:
    if TOKEN:
        return {"Authorization": f"Bearer {TOKEN}"}
    return {}


def _client(**kwargs) -> httpx.AsyncClient:
    """httpx client with Sapphire-friendly defaults (self-signed cert OK)."""
    return httpx.AsyncClient(verify=VERIFY_SSL, **kwargs)


async def _hammer_one(client: httpx.AsyncClient, text: str, idx: int) -> dict:
    """Run one /api/tts/stream request. Collect SSE events. Return summary."""
    result = {
        "idx": idx,
        "status": None,
        "stream_start_count": 0,
        "chunk_count": 0,
        "stream_end_count": 0,
        "notice_count": 0,
        "error_count": 0,
        "first_chunk_ms": None,
        "total_ms": None,
        "error": None,
    }
    import time
    t0 = time.monotonic()
    try:
        async with client.stream(
            "POST", f"{SAPPHIRE_URL}/api/tts/stream",
            json={"text": text},
            headers=_auth_headers(),
            timeout=120.0,
        ) as resp:
            result["status"] = resp.status_code
            if resp.status_code != 200:
                body = await resp.aread()
                result["error"] = f"HTTP {resp.status_code}: {body[:200].decode(errors='replace')}"
                return result
            buf = b""
            async for raw in resp.aiter_bytes():
                buf += raw
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "tts_stream_start":
                        result["stream_start_count"] += 1
                    elif t == "tts_chunk":
                        result["chunk_count"] += 1
                        if result["first_chunk_ms"] is None:
                            result["first_chunk_ms"] = int(
                                (time.monotonic() - t0) * 1000
                            )
                    elif t == "tts_stream_end":
                        result["stream_end_count"] += 1
                    elif t == "notice":
                        result["notice_count"] += 1
                    elif t == "error":
                        result["error_count"] += 1
    except httpx.TimeoutException:
        result["error"] = "request timeout"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    result["total_ms"] = int((time.monotonic() - t0) * 1000)
    return result


async def _run_scenario_c(scenario: scenarios.Scenario) -> dict:
    """Run one Lane C scenario. Return aggregated result."""
    headers = _auth_headers()

    async with _client(timeout=120.0) as client:
        # Spawn `concurrency` parallel requests
        tasks = [
            _hammer_one(client, scenario.request_text, i)
            for i in range(scenario.concurrency)
        ]
        sub_results = await asyncio.gather(*tasks, return_exceptions=False)

    # Aggregate
    all_statuses = [r["status"] for r in sub_results]
    chunks_total = sum(r["chunk_count"] for r in sub_results)
    notices_total = sum(r["notice_count"] for r in sub_results)

    # Invariants for Lane C — observe what's robust:
    #  - HTTP status matches expectation (200 for valid text, 400 for empty)
    #  - SSE structure intact for 200 responses
    #  - No 5xx (server crash / unhandled exception)
    #  - For unsynthesizable content (CJK/emoji): no crash, server handles
    #    gracefully (either by synth attempt OR by silent skip OR by notice).
    #    Behavior varies by Kokoro model version / language pack — we don't
    #    pin which response is "right", just that the request lands cleanly.
    expected_status = 200
    expected_to_error = False
    if not scenario.request_text.strip():
        expected_status = 400  # empty text
        expected_to_error = True   # _hammer_one flags non-200 as errored

    # Count only UNEXPECTED errors (a 400 for empty-text is expected)
    unexpected_errors = [r for r in sub_results
                         if r["error"] and not expected_to_error]

    invariants = {}
    status_ok = all(s == expected_status for s in all_statuses)
    invariants["status_codes"] = (
        status_ok,
        f"all returned {expected_status}: {all_statuses}",
    )

    # No 5xx anywhere — server must not crash even on weird input
    has_5xx = any(s is not None and s >= 500 for s in all_statuses)
    invariants["no_server_crash"] = (
        not has_5xx,
        f"no 5xx responses: {all_statuses}",
    )

    if expected_status == 200:
        all_have_start = all(r["stream_start_count"] >= 1 for r in sub_results)
        all_have_end = all(r["stream_end_count"] >= 1 for r in sub_results)
        invariants["sse_structure"] = (
            all_have_start and all_have_end,
            f"all requests got start+end SSE events "
            f"(starts={[r['stream_start_count'] for r in sub_results]}, "
            f"ends={[r['stream_end_count'] for r in sub_results]})",
        )

        # For "edge content" (CJK, emoji), accept any of: chunks emitted,
        # silent skip (0 chunks no notice — Kokoro filtered), or drop notice.
        # All are valid responses. We just want NO crash and stream completion.
        if "cjk" in scenario.name or "emoji" in scenario.name:
            invariants["edge_content_handled"] = (
                True,
                f"chunks={chunks_total}, notices={notices_total} "
                f"(any combo OK for {scenario.name})",
            )
        else:
            # The honest-system invariant: every request must produce EITHER
            # at least one chunk OR a drop notice. A 200-OK with zero chunks
            # AND zero notices means the system silently lied to the user.
            # That's the no-audio bug class we fixed in May 2026.
            silent_failures = [
                r for r in sub_results
                if r["chunk_count"] == 0 and r["notice_count"] == 0
            ]
            invariants["no_silent_failure"] = (
                not silent_failures,
                f"every request produced chunks OR a drop notice "
                f"(silent_failures={len(silent_failures)}/{len(sub_results)}, "
                f"chunks_total={chunks_total}, notices_total={notices_total})",
            )

    return {
        "scenario": scenario.name,
        "lane": "c",
        "ok": all(p for p, _ in invariants.values()) and not unexpected_errors,
        "invariants": {k: {"ok": v[0], "msg": v[1]} for k, v in invariants.items()},
        "sub_results": sub_results,
        "errored": len(unexpected_errors),
        "chunks_total": chunks_total,
        "notices_total": notices_total,
    }


async def run_lane_c(only: Optional[str] = None) -> list:
    """Run all Lane C scenarios. Returns list of result dicts."""
    if not TOKEN:
        print("\n  Lane C SKIPPED: MJOLNIR_API_TOKEN env var not set.")
        print("  Setup:")
        print("    1. Open Sapphire UI → Settings → System → API Keys")
        print("    2. Click '+ Add', name it 'mjolnir', save the token")
        print("    3. export MJOLNIR_API_TOKEN=sk_xxxxxxxxxxxx")
        return []

    # Quick connectivity + auth probe
    try:
        async with _client(timeout=5.0) as client:
            r = await client.get(f"{SAPPHIRE_URL}/api/status",
                                  headers=_auth_headers())
            if r.status_code == 401 or r.status_code == 403:
                print(f"\n  Lane C SKIPPED: auth failed "
                      f"(MJOLNIR_API_TOKEN rejected, status={r.status_code})")
                return []
            if r.status_code >= 500:
                print(f"\n  Lane C SKIPPED: Sapphire unhealthy at {SAPPHIRE_URL} "
                      f"(status={r.status_code})")
                return []
    except httpx.ConnectError:
        print(f"\n  Lane C SKIPPED: Sapphire unreachable at {SAPPHIRE_URL}")
        return []

    results = []
    for scenario in scenarios.scenarios_for_lane("c"):
        if only and scenario.name != only:
            continue
        print(f"  ► [{scenario.name}] {scenario.description}")
        result = await _run_scenario_c(scenario)
        results.append(result)
        for name, inv in result["invariants"].items():
            sym = "✓" if inv["ok"] else "✗"
            print(f"    {sym} {name}: {inv['msg']}")
        if result["errored"]:
            print(f"    ! {result['errored']} requests errored")
    return results


if __name__ == "__main__":
    asyncio.run(run_lane_c())
