"""Add (or redo) the AI featured image on an existing newsdesk draft.

    python -m newsdesk illustrate --post 5833            # only if it has no image
    python -m newsdesk illustrate --post 5833 --replace  # make a new one anyway

Only drafts and pending posts are touched; published posts are left alone."""

from __future__ import annotations

import html
import json
import logging
import re

from . import images, prompts, render
from .llm import Claude
from .wordpress import WordPress

log = logging.getLogger(__name__)

EDITABLE_STATUSES = {"draft", "pending", "future", "private"}
LEADING_IMAGE = re.compile(r"^\s*<!-- wp:image\b.*?<!-- /wp:image -->\s*", re.DOTALL)


def illustrate_post(cfg, http, post_id: int, replace: bool = False, wp: WordPress | None = None,
                    claude: Claude | None = None) -> dict:
    wp = wp or WordPress(cfg.site_url, cfg.secrets.wp_user, cfg.secrets.wp_app_password, http)
    post = wp.get_post(post_id)
    title = (post.get("title") or {}).get("raw") or ""
    status = post.get("status")
    if status not in EDITABLE_STATUSES:
        return {"post_id": post_id, "title": title, "result": f"skipped: post is {status}, not a draft"}
    if post.get("featured_media") and not replace:
        return {"post_id": post_id, "title": title, "result": "skipped: already has a featured image"}

    meta = post.get("meta") or {}
    try:
        report = json.loads(meta.get("_newsdesk_report") or "{}")
    except (TypeError, ValueError):
        report = {}

    scene, alt = report.get("image_prompt", ""), report.get("image_alt", "")
    if not scene or not alt:
        content_text = html.unescape(re.sub(r"<[^>]+>", " ", (post.get("content") or {}).get("raw", "")))
        excerpt = (post.get("excerpt") or {}).get("raw", "")
        claude = claude or Claude(cfg.secrets.anthropic_api_key, http)
        brief = claude.json(cfg["models"]["fact_check"], prompts.IMAGE_BRIEF_SYSTEM,
                            prompts.image_brief_user(title, excerpt, re.sub(r"\s+", " ", content_text)),
                            prompts.IMAGE_BRIEF_SCHEMA, max_tokens=1500)
        scene = scene or brief["prompt"]
        alt = alt or brief["alt_text"]
    alt = alt[:125]

    img = images.generate(cfg["images"], cfg.secrets.openai_api_key, http, scene)
    if img is None:
        raise RuntimeError("Image generation is switched off or OPENAI_API_KEY is missing")
    slug = post.get("slug") or f"post-{post_id}"
    caption = cfg["images"]["caption"]
    media = wp.upload_media(img.data, f"{slug}.{img.ext}", img.mime, alt, caption, title,
                            description=img.prompt)
    media_url = (media.get("media_details", {}).get("sizes", {}).get("large", {}).get("source_url")
                 or media.get("source_url", ""))

    content = (post.get("content") or {}).get("raw", "")
    block = render.image(media["id"], media_url, alt, caption)
    if cfg["site"].get("embed_image_in_content", True):
        content = block + "\n\n" + (LEADING_IMAGE.sub("", content, count=1) if replace else content.lstrip())

    payload: dict = {"featured_media": media["id"], "content": content}
    if "_newsdesk_report" in meta:
        report["warnings"] = [w for w in report.get("warnings", []) if "featured image" not in w.lower()]
        report["image_prompt"], report["image_alt"] = scene, alt
        payload["meta"] = {"_newsdesk_report": json.dumps(report)}
    wp.update_post(post_id, payload)
    wp.attach_media(media["id"], post_id)
    log.info("Added featured image %s to post %s", media["id"], post_id)
    return {"post_id": post_id, "title": title, "result": "image added", "media_id": media["id"],
            "image_url": media_url, "alt_text": alt, "edit_link": wp.edit_link(post_id)}
