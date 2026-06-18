"""Core integrity checker (core/integrity.py) + the manifest-staleness guard.

The checker hashes the files LISTED in core_manifest.json and reports missing/mismatched
files — catching partial updates, half-applied pulls, corruption. Unsigned SHA256 by design.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import integrity


def test_verify_clean_tree_is_ok():
    """The committed manifest must verify clean against the real tree."""
    r = integrity.verify()
    assert r["available"] is True
    assert r["ok"] is True, f"unexpected drift: missing={r['missing']} mismatched={r['mismatched']}"
    assert r["matched"] == r["total"] > 0


def test_verify_flags_mismatch_and_missing(tmp_path, monkeypatch):
    real = "VERSION"
    correct = integrity._hash_file(integrity.ROOT / real)
    manifest = {"version": "test", "files": {
        real: correct,                          # present + correct -> matched
        "core/integrity.py": "0" * 64,          # present + wrong hash -> mismatched
        "core/does_not_exist_xyz.py": "0" * 64,  # absent -> missing
    }}
    mpath = tmp_path / "core_manifest.json"
    mpath.write_text(integrity.manifest_json(manifest))
    monkeypatch.setattr(integrity, "MANIFEST_PATH", mpath)

    r = integrity.verify()
    assert r["ok"] is False
    assert "core/integrity.py" in r["mismatched"]
    assert "core/does_not_exist_xyz.py" in r["missing"]
    assert real not in r["mismatched"] and real not in r["missing"]  # the good one passed
    assert r["matched"] == 1


def test_verify_missing_manifest_is_graceful(tmp_path, monkeypatch):
    """A missing/unreadable manifest must report available=False, never crash."""
    monkeypatch.setattr(integrity, "MANIFEST_PATH", tmp_path / "nope.json")
    r = integrity.verify()
    assert r["ok"] is False and r["available"] is False
    assert r["total"] == 0


def test_repair_nothing_to_do_on_clean_tree():
    r = integrity.repair()
    assert r["repaired"] == [] and r["failed"] == []
    assert r["reverify"]["ok"] is True


def test_core_manifest_is_current():
    """REGRESSION GUARD: the committed manifest must match the current tracked tree.

    If this is RED, you edited tracked files without regenerating the manifest. Run
    `python tools/generate_core_manifest.py` and commit the result before pushing —
    otherwise users get false 'mismatch' alarms."""
    committed = integrity.load_manifest()
    assert committed is not None, "core_manifest.json missing"
    fresh = integrity.build_manifest()
    assert committed.get("files") == fresh["files"], (
        "core_manifest.json is STALE - run tools/generate_core_manifest.py and commit it"
    )
