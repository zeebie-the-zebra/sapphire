# plugins/remembrance/ops.py — offsite backup operations, shared by the tool, the
# settings routes, and the cron handler. Produces an ENCRYPTED blob (encrypt-or-
# refuse) and ships it to the zero-knowledge vault.
import logging
import shutil
import tempfile
import time
from pathlib import Path

import requests

from core import backup_crypto
from core import restore as restore_mod
from core.backup import backup_manager
from core.credentials_manager import credentials
from core.plugin_loader import plugin_loader
from plugins.remembrance import client

logger = logging.getLogger(__name__)
PLUGIN = "remembrance"
CADENCES = ("daily", "weekly", "monthly", "manual")


def _state():
    return plugin_loader.get_plugin_state(PLUGIN)


PREF_DEFAULTS = {"offsite_extra_patterns": [], "offsite_max_mb": 2048,
                 "offsite_cron_hour": None, "auto_enabled": False}


def get_prefs():
    """Plugin prefs (stored in PluginState, written by the settings panel)."""
    try:
        cfg = _state().get("config", {}) or {}
    except Exception:
        cfg = {}
    return {**PREF_DEFAULTS, **cfg}


def _set_last_result(ok, message):
    try:
        _state().save("last_result", {"ok": bool(ok), "message": message,
                                      "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    except Exception:
        pass


def last_result():
    try:
        return _state().get("last_result", None)
    except Exception:
        return None


def _account():
    """Fully-configured vault account, or None."""
    acct = credentials.get_offsite_account()
    if acct.get("server_url") and acct.get("tenant_id") and acct.get("api_key"):
        return acct
    return None


def _offsite_password():
    """v1: reuse the global backup password (encrypt-or-refuse)."""
    return credentials.get_backup_password() or ""


def _extra_patterns():
    raw = get_prefs().get("offsite_extra_patterns") or []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.splitlines() if p.strip()]
    return [str(p).strip() for p in raw if str(p).strip()]


def _err_from_http(e):
    code = getattr(getattr(e, "response", None), "status_code", None)
    return {
        400: "Bad request (cadence?)",
        401: "Bad tenant ID or API key",
        404: "Not found on the vault",
        410: "Vault lost the file (corruption)",
        413: "Vault is full / backup exceeds your quota",
        422: "Upload integrity check failed",
    }.get(code, f"Vault error ({code})" if code else f"Network error: {e}")


def perform_offsite_backup(cadence="daily", comment=""):
    """Create an encrypted blob (page + offsite excludes) and upload it.
    Returns {ok, id, size_bytes, usage_bytes, quota_bytes, ...} or {ok:False, error}."""
    if cadence not in CADENCES:
        return {"ok": False, "error": f"Invalid cadence '{cadence}'"}
    acct = _account()
    if not acct:
        return {"ok": False, "error": "Remembrance is not configured (set server URL, tenant ID, API key)"}
    pw = _offsite_password()
    if not pw:
        return {"ok": False, "error": "Offsite requires encryption — set a backup password on the Backup page first"}

    extra = _extra_patterns()
    cap_mb = int(get_prefs().get("offsite_max_mb", 2048) or 0)
    # Runaway guard: refuse before building a giant local blob (the 150 GB war story).
    try:
        est = backup_manager.estimate_size(extra_patterns=extra)
        if cap_mb > 0 and est.get("total_bytes", 0) > cap_mb * 1024 * 1024:
            msg = (f"Backup is ~{est['total_bytes'] // (1024 * 1024)} MB (cap {cap_mb} MB) — "
                   f"add offsite excludes or raise the cap")
            _set_last_result(False, msg)
            return {"ok": False, "error": msg}
    except Exception as e:
        logger.warning(f"[remembrance] size estimate failed (continuing): {e}")

    # Build the blob in a temp dir UNDER user_backups/ (a disk sibling of user/,
    # never itself backed up) — never inside user/ (would loop) and never /tmp
    # (could be RAM/tmpfs for a big blob).
    tmp = Path(tempfile.mkdtemp(prefix="remembrance_", dir=str(backup_manager.backup_dir)))
    try:
        fn = backup_manager.create_backup(backup_type="offsite", extra_patterns=extra,
                                          password=pw, dest_dir=tmp)
        if not fn:
            msg = "Backup produced no file (empty after excludes?)"
            _set_last_result(False, msg)
            return {"ok": False, "error": msg}
        try:
            res = client.upload(acct, tmp / fn, cadence, comment=comment)
        except requests.HTTPError as e:
            msg = _err_from_http(e)
            _set_last_result(False, msg)
            return {"ok": False, "error": msg}
        except requests.RequestException as e:
            msg = f"Network error: {e}"
            _set_last_result(False, msg)
            return {"ok": False, "error": msg}
        kb = res.get("size_bytes", 0) // 1024
        _set_last_result(True, f"Uploaded {cadence} backup ({kb} KB)" + (f" — {comment}" if comment else ""))
        return {"ok": True, **res}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def get_status():
    """Vault usage/quota + backup list, or {ok:False, error}."""
    acct = _account()
    if not acct:
        return {"ok": False, "configured": False, "error": "not configured"}
    try:
        data = client.list_backups(acct)
    except requests.HTTPError as e:
        return {"ok": False, "configured": True, "error": _err_from_http(e)}
    except requests.RequestException as e:
        return {"ok": False, "configured": True, "error": f"Network error: {e}"}
    return {"ok": True, "configured": True, **data}


def stage_restore(backup_id=None, cadence=None, password=None):
    """Download (latest or by id) → verify sha → decrypt → validate → stage for the
    watchdog swap. Returns the archive roots. The CALLER fires the restart.
    `password` lets a user restore on a NEW machine (the stored one won't be there)."""
    acct = _account()
    if not acct:
        raise ValueError("Remembrance is not configured")
    pw = password or _offsite_password()
    if not pw:
        raise ValueError("Backup password required to decrypt the offsite backup")
    restore_mod.RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    enc = restore_mod.RESTORE_DIR / "offsite_download.sapphirebak"
    dec = restore_mod.RESTORE_DIR / "offsite_decrypted.tar.gz"
    try:
        client.download(acct, enc, backup_id=backup_id, cadence=cadence)   # verifies sha256
        if backup_crypto.is_encrypted_backup(enc):
            backup_crypto.decrypt_file(enc, dec, pw)                       # ValueError on wrong pw
        else:
            shutil.copy2(enc, dec)
        roots = restore_mod.validate_tar(dec)
        restore_mod.request_restore(dec, source=f"remembrance:{backup_id or 'latest'}", trusted=True)
        return roots
    finally:
        enc.unlink(missing_ok=True)
        if dec.exists():       # success moved it to STAGED; this cleans the failure case
            dec.unlink()


def delete_backup(backup_id):
    acct = _account()
    if not acct:
        return {"ok": False, "error": "not configured"}
    try:
        return {"ok": True, **client.delete(acct, backup_id)}
    except requests.HTTPError as e:
        return {"ok": False, "error": _err_from_http(e)}
    except requests.RequestException as e:
        return {"ok": False, "error": f"Network error: {e}"}


def test_connection():
    acct = _account()
    if not acct:
        return {"ok": False, "error": "not configured"}
    try:
        client.health(acct["server_url"])
        client.list_backups(acct)          # also exercises auth
        return {"ok": True}
    except requests.HTTPError as e:
        return {"ok": False, "error": _err_from_http(e)}
    except requests.RequestException as e:
        return {"ok": False, "error": f"Network error: {e}"}
