"""Tests for the runtime LOG_LEVEL setting and its hot-reload behavior.

Verifies:
- set_log_level(name) updates root logger level.
- uvicorn.access stays pinned at WARNING regardless of user-selected level
  (per the Fork 1-B design decision — HTTP access-log noise must not bleed
  into DEBUG when a user flips for diagnostic visibility).
- Invalid names are handled defensively (keep current level, don't crash).
- Case-insensitive name parsing ("debug" == "DEBUG").
- None falls back to INFO default.
"""
import logging
import pytest

from core.sapphire_logging import set_log_level


@pytest.fixture
def restore_log_level():
    """Snapshot root logger level + uvicorn.access level, restore after test."""
    root = logging.getLogger()
    uvicorn_access = logging.getLogger("uvicorn.access")
    original_root = root.level
    original_uvicorn = uvicorn_access.level
    yield
    root.setLevel(original_root)
    uvicorn_access.setLevel(original_uvicorn)


def test_set_log_level_updates_root_logger(restore_log_level):
    root = logging.getLogger()
    set_log_level("DEBUG")
    assert root.level == logging.DEBUG
    set_log_level("WARNING")
    assert root.level == logging.WARNING
    set_log_level("INFO")
    assert root.level == logging.INFO
    set_log_level("ERROR")
    assert root.level == logging.ERROR


def test_set_log_level_keeps_uvicorn_access_at_warning(restore_log_level):
    """Even when root is dropped to DEBUG, uvicorn.access must stay WARNING.
    Otherwise every HTTP request would log and drown out the actual
    diagnostic content the user flipped to DEBUG to see."""
    set_log_level("DEBUG")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    set_log_level("INFO")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_set_log_level_invalid_name_keeps_current(restore_log_level):
    """Garbage value must NOT crash — log a warning, keep current level."""
    root = logging.getLogger()
    set_log_level("INFO")  # known starting state
    set_log_level("PURPLE_GIRAFFE")
    assert root.level == logging.INFO, "invalid name silently changed level"


def test_set_log_level_none_defaults_to_info(restore_log_level):
    """None / empty string falls back to INFO."""
    root = logging.getLogger()
    set_log_level("DEBUG")  # change away from INFO first
    set_log_level(None)
    assert root.level == logging.INFO
    set_log_level("DEBUG")
    set_log_level("")
    assert root.level == logging.INFO


def test_set_log_level_case_insensitive(restore_log_level):
    """User-facing values may come from a dropdown as 'DEBUG' or 'debug' —
    both must work the same way."""
    root = logging.getLogger()
    set_log_level("debug")
    assert root.level == logging.DEBUG
    set_log_level("warning")
    assert root.level == logging.WARNING
    set_log_level("Info")  # mixed case
    assert root.level == logging.INFO


def test_settings_callback_wired_for_log_level():
    """The settings reload callback for LOG_LEVEL is registered when the
    VoiceChatSystem boots. We can't easily fake-boot the system here, but
    we CAN verify the callback registration API works the way the sapphire.py
    initialization assumes: register a callback, settings.set fires it."""
    from core.settings_manager import settings as _settings

    received = []
    def _callback(value):
        received.append(value)

    # Register, then trigger via settings.set
    _settings.register_reload_callback("LOG_LEVEL", _callback)
    try:
        _settings.set("LOG_LEVEL", "WARNING", persist=False)
        assert received == ["WARNING"], f"callback not fired or got wrong value: {received}"

        _settings.set("LOG_LEVEL", "INFO", persist=False)
        assert received == ["WARNING", "INFO"]
    finally:
        # Cleanup — remove our callback
        _settings._reload_callbacks.pop("LOG_LEVEL", None)
        # Restore default
        _settings.set("LOG_LEVEL", "INFO", persist=False, _skip_callbacks=True)
