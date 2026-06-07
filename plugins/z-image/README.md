# z-image — Z-Image Turbo image generation

Fast local text-to-image for Sapphire via **sd-server** (stable-diffusion.cpp) running
**Z-Image Turbo**. Sapphire can generate several images at once, **see them herself**
(as a labeled grid), and **recreate any of them exactly** by seed.

> This is a separate plugin. The older SDXL `image-gen` plugin is untouched.

## What it does

One tool — `generate_image`:

- **`prompt`** (required) — what to draw.
- **`count`** (default 1) — generate several at once. With `count > 1` you get a single
  **numbered grid** the model can see for comparison, plus each image full-size for the user.
- **`seed`** (omit = random) — set it to reproduce an exact image from a prior result.
- **`width` / `height`** (1024) · **`steps`** (8) · **`cfg_scale`** (1.0) · **`negative_prompt`** ("") —
  all optional, Z-Image-Turbo-tuned defaults, overridable.

Every result includes a **recipe**: the prompt, params, and each image's seed — so
"recreate #3" is just `generate_image(prompt=…, seed=<#3's seed>, …)`.

The image(s) flow back through Sapphire's core tool-image path, so a **vision-capable
model sees them**; on a text-only model they're still saved/shown to the user and the
model gets a short CLIP description instead.

## Requirements

- An **sd-server** (stable-diffusion.cpp) instance serving Z-Image Turbo, reachable at the
  configured **sd-server URL** (default `http://127.0.0.1:7861`). See the `sd-server`
  side-service setup (build/run/systemd) for standing one up on the GPU box.
- For Sapphire to *see* her own images, run her on a **vision-capable model**.

## Settings

| Setting | Default | Notes |
|---|---|---|
| sd-server URL | `http://127.0.0.1:7861` | where sd-server listens |
| Max images per call | 6 | protects VRAM/time |
| Default steps / CFG | 8 / 1.0 | Z-Image Turbo tuning |
| Default negative prompt | (none) | Z-Image needs little |
| Per-image timeout | 180s | per single image |

## Notes

- Seeds are assigned client-side, so the recipe is always complete and reproducible
  regardless of what the server reports.
- Reproduction is deterministic at a fixed seed + params (may differ trivially if the
  server's attention backend isn't bit-exact).
