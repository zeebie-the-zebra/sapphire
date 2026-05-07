"""Dashboard widget registry — built-ins and plugin widgets share this shape.

The registry is an in-memory map keyed by (plugin, widget_id). Built-ins
register at boot via core/dashboard_builtins/. Plugin widgets register
when the plugin loader processes their manifest. Both go through
register_widget(spec) — same contract.

Used by:
  - GET /api/dashboard/widgets/available  — picker catalog
  - dashboard.js host — fetches render_url for each user-placed panel
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class WidgetSpec:
    """Declares a dashboard panel widget. Minimal V1 surface — fields will
    grow as the API adds capabilities (mood signals, etc.)."""
    plugin: str            # "core" for built-ins, plugin name otherwise
    widget_id: str         # unique within plugin
    name: str
    render_url: str        # browser fetches this for the render() function
    description: str = ""
    icon: str = ""
    sizes: list[str] = field(default_factory=lambda: ["1x1"])
    default_size: str = "1x1"
    multi_instance: bool = False
    api_version: int = 1

    def to_public_dict(self) -> dict:
        """Shape sent to the frontend picker catalog."""
        return asdict(self)


# plugin_name -> widget_id -> spec
_registry: dict[str, dict[str, WidgetSpec]] = {}


def register_widget(spec: WidgetSpec) -> None:
    """Add a widget to the registry. Re-registering replaces the prior entry
    (so plugin hot-reload swaps cleanly)."""
    _registry.setdefault(spec.plugin, {})[spec.widget_id] = spec


def get_widget(plugin: str, widget_id: str) -> Optional[WidgetSpec]:
    return _registry.get(plugin, {}).get(widget_id)


def list_widgets() -> list[WidgetSpec]:
    """All registered widgets, in registration order within each plugin.
    Built-ins typically appear first because they register at app boot."""
    return [s for plug in _registry.values() for s in plug.values()]


def unregister_plugin_widgets(plugin: str) -> None:
    """Drop all widgets for a plugin. Called when a plugin unloads."""
    _registry.pop(plugin, None)
