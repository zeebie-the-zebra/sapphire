# Settings

Plugins can declare settings that render automatically in the web UI — no JavaScript needed. For complex interactive UIs, a custom web module is also supported.

## Manifest-Declared Settings

Declare settings in `plugin.json` and they auto-render in Settings > Plugins:

```json
"capabilities": {
  "settings": [
    {"key": "api_key", "type": "string", "label": "API Key", "default": "", "widget": "password", "help": "Your API key"},
    {"key": "units", "type": "string", "label": "Units", "default": "metric", "options": [{"label": "Metric", "value": "metric"}, {"label": "Imperial", "value": "imperial"}]},
    {"key": "cache_min", "type": "number", "label": "Cache (min)", "default": 15},
    {"key": "enabled", "type": "boolean", "label": "Enabled", "default": true}
  ]
}
```

### Field Schema

| Field | Required | Description |
|-------|----------|-------------|
| `key` | yes | Setting key (unique within plugin) |
| `type` | yes | `"string"`, `"number"`, `"boolean"` |
| `label` | yes | Display name |
| `default` | yes | Default value |
| `help` | no | Description text |
| `widget` | no | Override: `"textarea"`, `"password"`, `"select"`, `"radio"`, `"button"` (action button) |
| `options` | no | `[{label, value}]` for select/radio |
| `placeholder` | no | Input hint text |
| `confirm` | no | Danger confirm gate (see below) |

Widget inference when omitted: `string` -> text, `string` + `options` -> select, `number` -> number spinner, `boolean` -> toggle, `textarea` type -> textarea.

### Danger Confirm

Any field can have a `confirm` object that shows a danger dialog when a specific value is selected:

```json
{
    "key": "validation", "type": "string", "label": "Validation", "default": "moderate",
    "confirm": {
        "values": ["trust"],
        "title": "Trust Mode",
        "warnings": ["Warning 1", "Warning 2"],
        "buttonLabel": "Enable Trust Mode"
    }
}
```

### Storage

Settings are stored at `user/webui/plugins/{name}.json` and read via `plugin_loader.get_plugin_settings(name)` (merges stored values with manifest defaults).

---

## Custom Web Settings UI

For settings that need custom JavaScript beyond what manifest settings provide, plugins can ship a `web/` subdirectory.

**Most plugins should use manifest `settings` instead** — it's simpler and requires no JavaScript. Use `web` only for complex interactive UIs.

### Manifest

```json
"capabilities": {
  "web": {
    "settingsUI": "plugin"
  }
}
```

### Structure

```
plugins/my-plugin/
  plugin.json
  web/
    index.js               # Entry point (required)
    style.css              # Optional
```

Assets served at `/plugin-web/my-plugin/index.js`.

### index.js Contract

```javascript
import { registerPluginSettings } from '/static/shared/plugin-registry.js';
import pluginsAPI from '/static/shared/plugins-api.js';

export default {
  name: 'my-plugin',

  init(container) {
    registerPluginSettings({
      id: 'my-plugin',
      name: 'My Plugin',
      icon: '⚙️',
      helpText: 'Configure my plugin',

      render(container, settings) {
        container.innerHTML = `
          <input type="text" id="mp-url" value="${settings.url || ''}">
        `;
      },

      load: () => pluginsAPI.getSettings('my-plugin'),
      save: (settings) => pluginsAPI.saveSettings('my-plugin', settings),

      getSettings(container) {
        return { url: container.querySelector('#mp-url').value };
      }
    });
  },

  destroy() { }
};
```

### Global Save vs self-saving panels

`getSettings` + `save` are how your panel participates in the Settings view's
global **Save Changes** button: it calls `getSettings(container)` and passes the
result to `save(settings)`.

**If your panel persists through its own buttons** (account managers, per-item
CRUD editors — anything using `createAccountManager`), **omit `getSettings` and
`save` entirely.** The global Save button then hides on your tab and shows
"This page saves with its own buttons" instead.

Never register stub callbacks like `getSettings: () => ({})` with a no-op
`save` — the global button will report "settings saved" while writing nothing,
and users lose edits believing they saved (the 2026-07-04 two-save-buttons
bug, which shipped in eight plugins by copy-paste before being caught).

---

## Settings API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/webui/plugins/{name}/settings` | Read settings |
| PUT | `/api/webui/plugins/{name}/settings` | Save settings |
| DELETE | `/api/webui/plugins/{name}/settings` | Reset to defaults |
