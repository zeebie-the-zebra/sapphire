# HTTP Routes

Plugins can register custom HTTP endpoints. Auth, CSRF, and rate limiting are enforced by the framework — your handler code never touches any of that.

## Manifest Declaration

```json
{
  "capabilities": {
    "routes": [
      {
        "method": "POST",
        "path": "capture/{request_id}",
        "handler": "routes/capture.py:handle_capture"
      }
    ]
  }
}
```

The full URL becomes: `POST /api/plugin/{plugin_name}/{path}`

For the example above: `POST /api/plugin/webcam/capture/abc123`

## Route Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `method` | string | No | `GET`, `POST`, `PUT`, or `DELETE` (default: `GET`) |
| `path` | string | Yes | URL path (supports `{param}` placeholders) |
| `handler` | string | Yes | `file:function` reference (default function: `handle`) |

## Handler Signature

The framework calls your handler with **keyword arguments**: every `{name}` path param, plus `body`, `settings`, `credentials`, `query`, and `request`. Always end your signature with `**_` to absorb the ones you don't use — otherwise the call raises `TypeError` on the first request (the framework always passes all of them).

```python
def handle_capture(request_id, body=None, settings=None, **_) -> dict:
    """
    Injected keyword args (declare only what you need, then **_):
        request_id:  path param extracted from {request_id}
        body:        parsed JSON body (POST/PUT; empty dict for GET/DELETE)
        settings:    plugin settings from user/webui/plugins/{name}.json
        credentials: the credentials manager (resolve secrets server-side)
        query:       dict of URL query-string params
        request:     the raw Starlette Request object

    Returns:
        dict (auto-serialized to JSON) or a FastAPI Response object
    """
    return {"status": "ok"}
```

Path parameters arrive as keyword arguments matching the `{name}` in your path pattern. Because `body`, `settings`, `credentials`, `query`, and `request` are **always** passed, your handler must accept them by name or swallow them with `**_`.

## Security

All of the following are enforced automatically — you cannot disable them:

- **Authentication**: `require_login` dependency — session or API key required
- **CSRF**: Middleware validates tokens on POST/PUT/DELETE from browser sessions
- **Rate limiting**: 60 GET / 30 non-GET requests per minute, per plugin (bucketed by session — or by bearer-token hash when bearer auth is used)
- **Bearer-token auth (opt-in)**: a plugin may *add* (never weaken) a bearer path by writing `user/plugin_state/{plugin}_mcp_key.json` (`{"key": "..."}`). A request whose `Authorization: Bearer <key>` matches then bypasses session login — used by MCP clients. Session CSRF still cannot be disabled.

## Example: Webcam Capture Endpoint

```
plugins/webcam/
  plugin.json
  routes/capture.py
  tools/webcam.py
```

**plugin.json:**
```json
{
  "name": "webcam",
  "version": "1.0.0",
  "capabilities": {
    "tools": ["tools/webcam.py"],
    "routes": [
      {
        "method": "POST",
        "path": "capture/{request_id}",
        "handler": "routes/capture.py:handle_capture"
      }
    ]
  }
}
```

**routes/capture.py:**
```python
import threading

# Pending capture requests: {request_id: {"event": Event, "image": None}}
_pending = {}
_lock = threading.Lock()

def create_request(request_id, timeout=15):
    """Called by the tool — blocks until browser POSTs the image."""
    event = threading.Event()
    with _lock:
        _pending[request_id] = {"event": event, "image": None}
    event.wait(timeout=timeout)
    with _lock:
        data = _pending.pop(request_id, {})
    return data.get("image")

def handle_capture(request_id: str, body: dict, **_) -> dict:
    """Called by the browser — delivers the captured image."""
    with _lock:
        req = _pending.get(request_id)
    if not req:
        return {"error": "No pending request"}
    req["image"] = body
    req["event"].set()
    return {"status": "ok"}
```

## Notes

- Routes are registered on plugin load and removed on unload
- Hot reload (`POST /api/plugins/{name}/reload`) re-registers routes
- Handlers can be sync or async — async handlers are awaited directly, sync handlers run in a threadpool
- Path parameters only match single path segments (no slashes)
