"""Z-Image Turbo image generation via sd-server (stable-diffusion.cpp).

One tool, `generate_image`. The model:
  - generates `count` images at once (each with its own seed),
  - SEES them (a single labeled grid when count>1, or the image when count==1) —
    this rides the core `{"text","images"}` tool-return path (chat_tool_calling
    ._extract_tool_images), so a vision-capable model views them and the user
    sees them rendered in chat,
  - gets a numbered RECIPE in the text so any image can be recreated exactly by
    calling generate_image again with that image's seed + params.

Seeds are assigned client-side, so the recipe is always complete regardless of
what the server reports back.
"""

import base64
import io
import logging
import math
import random
import re

logger = logging.getLogger(__name__)

ENABLED = True
EMOJI = "\U0001F5BC"  # 🖼️

_DEFAULTS = {
    "api_url": "http://127.0.0.1:7861",
    "max_count": 6,
    "default_steps": 8,
    "default_cfg": 1.0,
    "default_negative": "",
    "timeout": 180,
    "static_keywords": "",
    "character_descriptions": {"me": "", "you": ""},
}


def _expand_prompt(prompt, cfg):
    """SDXL-style convenience: replace whole-word 'me'/'you' (and any other
    configured markers) with physical descriptions, then append static keywords.
    The model writes 'me' for itself and 'you' for the human."""
    out = prompt
    chars = cfg.get("character_descriptions") or {}
    if isinstance(chars, dict):
        for marker, desc in chars.items():
            if marker and desc:
                out = re.sub(rf"\b{re.escape(marker)}\b", desc, out, count=1, flags=re.IGNORECASE)
    kw = (cfg.get("static_keywords") or "").strip()
    if kw:
        out = f"{out.rstrip('. ')}. {kw}".strip()
    return out


def _settings(plugin_settings=None):
    s = dict(_DEFAULTS)
    if plugin_settings:
        s.update({k: v for k, v in plugin_settings.items() if v is not None})
    else:
        try:
            from core.plugin_loader import plugin_loader
            stored = plugin_loader.get_plugin_settings("z-image") or {}
            s.update({k: v for k, v in stored.items() if v is not None})
        except Exception:
            pass
    return s


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image via Z-Image Turbo and optionally view it yourself. "
                "Describe it in ~20 words; write 'me' for yourself and 'you' for the human "
                "(auto-filled). Add the count param when you need multiple images."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The image description (~20 words)."},
                    "view": {
                        "type": "boolean",
                        "description": "User always sees the images; you view them too if true."
                    },
                    "count": {"type": "integer", "description": "How many at once (default 1)."},
                    "seed": {"type": "integer", "description": "Reproduce an exact image — use a seed from a prior result."},
                    "width": {"type": "integer", "description": "Pixels (default 1024)."},
                    "height": {"type": "integer", "description": "Pixels (default 1024)."},
                    "steps": {"type": "integer", "description": "(default 8)"},
                    "cfg_scale": {"type": "number", "description": "(default 1)"},
                    "negative_prompt": {"type": "string", "description": "What to avoid."}
                },
                "required": ["prompt"]
            }
        }
    }
]


def execute(function_name, arguments, config=None, plugin_settings=None, credentials=None):
    if function_name == "generate_image":
        return _exec_generate(arguments, plugin_settings)
    return f"Unknown function: {function_name}", False


def _call_sdserver(api_url, payload, timeout):
    """POST to sd-server's A1111-compatible txt2img. Returns image bytes or raises."""
    import requests
    url = api_url.rstrip("/") + "/sdapi/v1/txt2img"
    resp = requests.post(url, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"sd-server {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    # A1111 shape: {"images": ["<base64>", ...], "info": "..."}.
    # Defensive: tolerate a couple of common variants if the server differs.
    imgs = None
    if isinstance(data, dict):
        imgs = data.get("images") or data.get("data")
    if not imgs:
        raise RuntimeError(f"sd-server returned no images. Raw: {str(data)[:200]}")
    b64 = imgs[0]
    if isinstance(b64, dict):              # e.g. [{"b64_json": "..."}]
        b64 = b64.get("b64_json") or b64.get("data")
    if isinstance(b64, str) and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    return base64.b64decode(b64)


def _resize_for_chat(img_bytes, max_px=1536, quality=90):
    """Downscale + JPEG to keep chat/history light. Returns (jpeg_bytes)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes))
        if max(img.size) > max_px:
            r = max_px / max(img.size)
            img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return img_bytes


def _make_grid(images_bytes, cell=512):
    """Compose a numbered contact-sheet grid (1..N labels). Returns JPEG bytes."""
    from PIL import Image, ImageDraw, ImageFont
    n = len(images_bytes)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    grid = Image.new("RGB", (cols * cell, rows * cell), (24, 24, 28))
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(grid)
    for i, raw in enumerate(images_bytes):
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            continue
        im.thumbnail((cell, cell), Image.LANCZOS)
        x, y = (i % cols) * cell, (i // cols) * cell
        ox, oy = x + (cell - im.width) // 2, y + (cell - im.height) // 2
        grid.paste(im, (ox, oy))
        # number badge (top-left of cell)
        label = str(i + 1)
        draw.rectangle([x + 6, y + 6, x + 52, y + 52], fill=(0, 0, 0))
        draw.text((x + 18, y + 8), label, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    grid.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _exec_generate(arguments, plugin_settings=None):
    cfg = _settings(plugin_settings)

    prompt = (arguments.get("prompt") or "").strip()
    if not prompt:
        return "No prompt provided.", False

    try:
        max_count = int(cfg.get("max_count", 6))
    except (TypeError, ValueError):
        max_count = 6
    count = arguments.get("count", 1) or 1
    try:
        count = max(1, min(int(count), max_count))
    except (TypeError, ValueError):
        count = 1

    width = int(arguments.get("width") or 1024)
    height = int(arguments.get("height") or 1024)
    steps = int(arguments.get("steps") or cfg.get("default_steps", 8))
    cfg_scale = float(arguments.get("cfg_scale") if arguments.get("cfg_scale") is not None else cfg.get("default_cfg", 1.0))
    negative = arguments.get("negative_prompt")
    if negative is None:
        negative = cfg.get("default_negative", "")
    api_url = cfg.get("api_url", _DEFAULTS["api_url"])
    timeout = int(cfg.get("timeout", 180))

    # Client-assigned seeds → recipe is always complete + reproducible.
    base_seed = arguments.get("seed")
    if base_seed is not None:
        seeds = [int(base_seed) + i for i in range(count)]
    else:
        seeds = [random.randint(1, 2**31 - 1) for _ in range(count)]

    final_prompt = _expand_prompt(prompt, cfg)  # me/you swap + static keywords
    logger.info(f"[ZIMAGE] generating {count} @ {width}x{height} steps={steps} cfg={cfg_scale} seeds={seeds}")

    raw_images = []
    for seed in seeds:
        payload = {
            "prompt": final_prompt,
            "negative_prompt": negative,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "width": width,
            "height": height,
            "seed": seed,
            "batch_size": 1,
        }
        try:
            raw_images.append(_call_sdserver(api_url, payload, timeout))
        except Exception as e:
            logger.error(f"[ZIMAGE] generation failed (seed={seed}): {e}")
            if raw_images:
                break  # keep what we have; report the partial set
            return f"Image generation failed: {e}", False

    # ---- recipe text (travels WITH the images in the tool result) ----
    lines = [
        f"Generated {len(raw_images)} image(s) with Z-Image Turbo.",
        "To recreate any EXACTLY, call generate_image with that image's seed + these params:",
        f"  prompt: \"{prompt}\"",
        f"  size={width}x{height}  steps={steps}  cfg_scale={cfg_scale}"
        + (f"  negative=\"{negative}\"" if negative else ""),
        "",
    ]
    for i, seed in enumerate(seeds[:len(raw_images)], start=1):
        lines.append(f"  #{i}  seed={seed}")
    recipe = "\n".join(lines)

    # ---- build images for the return ----
    # The user ALWAYS sees the images. The model only sees them (vision tokens)
    # when it sets view=true — default is hands-off / cheaper.
    view = bool(arguments.get("view", False))
    def _enc(raw):
        return base64.b64encode(_resize_for_chat(raw)).decode()

    if len(raw_images) == 1:
        out_images = [{"data": _enc(raw_images[0]), "media_type": "image/jpeg",
                       "display_only": (not view)}]
    elif view:
        # labeled grid the model looks at, full-size individuals for the user
        out_images = [{"data": base64.b64encode(_make_grid(raw_images)).decode(),
                       "media_type": "image/jpeg"}]
        out_images += [{"data": _enc(r), "media_type": "image/jpeg", "display_only": True}
                       for r in raw_images]
    else:
        # model doesn't look — just show the user each full-size (no grid needed)
        out_images = [{"data": _enc(r), "media_type": "image/jpeg", "display_only": True}
                      for r in raw_images]

    return {"text": recipe, "images": out_images}, True
