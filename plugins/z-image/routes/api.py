"""z-image plugin routes — connection test for the sd-server backend."""

import logging

logger = logging.getLogger(__name__)


# POST /api/plugin/z-image/test   body: {"url": "http://host:7861"}
async def test_connection(**kwargs):
    """Reachability check: if sd-server answers ANY HTTP response at the base URL,
    it's connected. Only a connection error / timeout is a real failure. (We don't
    assume a specific API path — builds differ; generation uses /sdapi/v1/txt2img.)"""
    import requests

    request = kwargs.get("request")
    url = ""
    if request is not None:
        try:
            body = await request.json()
            url = (body.get("url") or "").strip()
        except Exception:
            url = ""
    if not url:
        return {"success": False, "error": "No URL provided"}
    if not url.startswith(("http://", "https://")):
        return {"success": False, "error": "URL must start with http:// or https://"}

    base = url.rstrip("/")
    try:
        # Any HTTP response means the host:port is alive and serving sd-server.
        requests.get(base + "/", timeout=6)
        return {"success": True}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "Could not connect (server down or wrong host/port?)"}
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Timed out reaching sd-server"}
    except Exception as e:
        return {"success": False, "error": str(e)}
