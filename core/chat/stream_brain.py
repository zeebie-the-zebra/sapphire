"""Per-stream 'brain' override (concurrent per-chat conversations).

A conversation stream saves messages to its target chat but historically resolved
its brain — LLM provider, persona/system-prompt, mind scopes, toolset — from the
session's single ACTIVE chat. That means a phone call (or any stream on a
non-active chat) borrowed the UI's active-chat brain.

This ContextVar lets a turn declare "resolve my brain from THIS chat instead."
It's a ContextVar, so it's isolated per execution context — a web-UI turn (asyncio)
and a phone-call turn (driver thread) each see their own value, no cross-bleed.

When unset (the common case — web UI on its active chat), everything falls back to
the session's active settings, so that path is byte-identical to before.

The override value is a dict: {"settings": <chat settings dict>,
"system_prompt": <base prompt str>, "tools": <tool list or None>, "chat": <name>}.
"""
from contextvars import ContextVar
from typing import Optional, Dict, Any

_override: ContextVar = ContextVar("stream_brain_override", default=None)


def get_override() -> Optional[Dict[str, Any]]:
    return _override.get()


def set_override(brain: Optional[Dict[str, Any]]):
    """Set for the current context. Returns a token for reset_override()."""
    return _override.set(brain)


def reset_override(token) -> None:
    try:
        _override.reset(token)
    except Exception:
        pass
