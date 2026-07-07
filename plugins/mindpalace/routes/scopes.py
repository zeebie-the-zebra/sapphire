# plugins/mindpalace/routes/scopes.py
# Scope CRUD for the sidebar memory dropdown (manifest scope decl points here).
# Response shapes mirror the classic /api/memory/scopes contract.

import re

_NAME_RE = re.compile(r'[^a-z0-9_-]+')


def _sanitize(raw):
    s = _NAME_RE.sub('', (raw or '').strip().lower().replace(' ', '-'))
    return s[:40]


def _tools():
    # Late import — palace_tools is installed in sys.modules by
    # register_plugin_tools before routes register (load order guarantee).
    from plugins.mindpalace.tools import palace_tools
    return palace_tools


def list_scopes(**_):
    return {"scopes": _tools().get_scopes()}


def create_scope(body=None, **_):
    name = _sanitize((body or {}).get('name'))
    if not name:
        return {"success": False, "detail": "Invalid scope name."}
    ok = _tools().create_scope(name)
    return {"success": bool(ok), "name": name}


def delete_scope(name=None, **_):
    result = _tools().delete_scope(name or '')
    if "error" in result:
        return {"success": False, "detail": result["error"]}
    return {"success": True, "deleted_count": result.get("deleted_count", 0)}
