"""Featured image generation (OpenAI Images API) and post-processing."""

from __future__ import annotations

import base64
import io
import json
import logging

from .http import HttpError

log = logging.getLogger(__name__)

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover - optional
    Image = None

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"

HOUSE_IMAGE_RULES = (
    "Editorial illustration for a shark news website, painterly-photographic style with "
    "natural underwater or coastal light, landscape composition with the subject slightly "
    "off-center and room around it for cropping. Anatomically accurate: correct fin shapes, "
    "gill-slit count, coloration and proportions for the species named. "
    "Absolutely no people or human faces, no text, letters, numbers, logos or watermarks, "
    "no blood, wounds or gore, no aggressive open-jaw 'attack' poses, no movie characters. "
    "Calm, respectful, documentary feel."
)


def build_prompt(scene: str) -> str:
    return f"{scene.strip().rstrip('.')}. {HOUSE_IMAGE_RULES}"


class ImageResult:
    def __init__(self, data: bytes, mime: str, ext: str, prompt: str):
        self.data, self.mime, self.ext, self.prompt = data, mime, ext, prompt


def generate(cfg_images: dict, api_key: str | None, http, scene: str) -> ImageResult | None:
    if cfg_images.get("provider", "openai") == "none":
        return None
    if not api_key:
        log.warning("OPENAI_API_KEY not set; skipping image generation")
        return None
    prompt = build_prompt(scene)
    payload = {
        "model": cfg_images.get("model", "gpt-image-2"),
        "prompt": prompt,
        "size": cfg_images.get("size", "1536x1024"),
        "quality": cfg_images.get("quality", "medium"),
        "n": 1,
    }
    resp = http.post(OPENAI_IMAGES_URL, timeout=240, data=json.dumps(payload),
                     headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    if resp.status_code != 200:
        message = resp.text[:400]
        try:
            message = resp.json()["error"]["message"]
        except Exception:
            pass
        raise HttpError(f"OpenAI said ({resp.status_code}): {message}", resp.status_code, resp.text)
    item = (resp.json().get("data") or [{}])[0]
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        raw = http.get(item["url"], timeout=120).content
    else:
        raise HttpError("OpenAI images returned no image data")
    return process(raw, cfg_images, prompt)


def process(raw: bytes, cfg_images: dict, prompt: str) -> ImageResult:
    """Resize and re-encode for the web. Falls back to the original PNG without Pillow."""
    fmt = (cfg_images.get("output_format") or "jpeg").lower()
    width = int(cfg_images.get("output_width") or 1200)
    if Image is None:
        return ImageResult(raw, "image/png", "png", prompt)
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    if img.width > width:
        height = round(img.height * width / img.width)
        img = img.resize((width, height), Image.LANCZOS)
    buf = io.BytesIO()
    if fmt == "webp":
        img.save(buf, "WEBP", quality=82, method=6)
        return ImageResult(buf.getvalue(), "image/webp", "webp", prompt)
    img.save(buf, "JPEG", quality=84, optimize=True, progressive=True)
    return ImageResult(buf.getvalue(), "image/jpeg", "jpg", prompt)
