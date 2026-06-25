"""[REGRESSION_GUARD] Backup exclude patterns + size estimate (Remembrance Stage 1).

Privacy floor always applies; user fnmatch globs add on top; the estimator sums
uncompressed sizes with a per-folder breakdown, and the preview uses the SAME
exclusion logic as the real tar filter.
"""
from core.backup import _is_excluded, _privacy_excluded, backup_manager


def test_privacy_floor_always_excluded():
    assert _privacy_excluded("plugin_state/foo.bad-123")
    assert _privacy_excluded("x.tmp")
    assert _privacy_excluded("a/b.tmp.456")
    assert _privacy_excluded("plugins/discord_mcp_key.json")
    assert _privacy_excluded("mcp_client.json")
    assert not _privacy_excluded("history/sapphire_history.db")


def test_is_excluded_user_globs():
    pats = ["rag/*", "*.log"]
    assert _is_excluded("rag/doc.pdf", pats)
    assert _is_excluded("rag/sub/deep.txt", pats)   # * crosses /
    assert _is_excluded("logs/app.log", pats)
    assert not _is_excluded("history/chat.db", pats)
    assert _is_excluded("x.tmp", [])                # privacy floor wins regardless
    # bare folder name excludes its whole subtree (the piper-voices case)
    assert _is_excluded("piper-voices/en_US.onnx", ["piper-voices"])
    assert _is_excluded("piper-voices", ["piper-voices"])
    assert not _is_excluded("piper-voices-extra/x", ["piper-voices"])  # no false prefix match


def test_estimate_size_with_exclusions(tmp_path, monkeypatch):
    user = tmp_path / "user"
    (user / "history").mkdir(parents=True)
    (user / "rag").mkdir(parents=True)
    (user / "history" / "chat.db").write_bytes(b"x" * 1000)
    (user / "rag" / "big.pdf").write_bytes(b"y" * 5000)
    (user / "notes.tmp").write_bytes(b"z" * 999)    # privacy floor

    monkeypatch.setattr(backup_manager, "user_dir", user)

    r = backup_manager.estimate_size(patterns=[])
    assert r["total_bytes"] == 6000
    assert r["excluded_bytes"] == 999
    # breakdown is by top-level entry (du --max-depth=1), sorted desc
    assert {b["name"]: b["bytes"] for b in r["breakdown"]} == {"rag": 5000, "history": 1000}

    r2 = backup_manager.estimate_size(patterns=["rag/*"])
    assert r2["total_bytes"] == 1000
    assert r2["excluded_bytes"] == 999 + 5000
    assert r2["breakdown"][0]["name"] == "history"
