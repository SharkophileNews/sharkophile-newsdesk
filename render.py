"""Turn the writer's structured body into WordPress block markup.

The model returns blocks (paragraph / heading / list / quote) whose text may
contain a few inline tags. We sanitize that inline HTML down to an allowlist
and build the Gutenberg comments ourselves, so the editor always opens a clean,
valid post."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

ALLOWED_INLINE = {"a", "em", "strong", "i", "b"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "i":
            tag = "em"
        if tag == "b":
            tag = "strong"
        if tag not in ALLOWED_INLINE:
            return
        if tag == "a":
            href = dict(attrs).get("href", "") or ""
            if urlparse(href).scheme not in ("http", "https"):
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}">')
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = {"i": "em", "b": "strong"}.get(tag.lower(), tag.lower())
        if tag in self.stack:
            # close any unclosed tags opened after this one
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_inline(text: str) -> str:
    # Turn stray markdown emphasis/links into HTML before sanitizing.
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text or "")
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    s = _Sanitizer()
    s.feed(text)
    s.close()
    return re.sub(r"\s+", " ", s.result()).strip()


def plain_text(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", sanitize_inline(text)))


# ------------------------------------------------------------------ blocks

def paragraph(text: str, italic: bool = False) -> str:
    inner = sanitize_inline(text)
    if italic:
        inner = f"<em>{inner}</em>"
    return f"<!-- wp:paragraph -->\n<p>{inner}</p>\n<!-- /wp:paragraph -->"


def heading(text: str, level: int = 2) -> str:
    attrs = "" if level == 2 else f' {{"level":{level}}}'
    return (f"<!-- wp:heading{attrs} -->\n<h{level} class=\"wp-block-heading\">"
            f"{sanitize_inline(text)}</h{level}>\n<!-- /wp:heading -->")


def bullet_list(items: list[str]) -> str:
    lis = "\n".join(f"<!-- wp:list-item -->\n<li>{sanitize_inline(i)}</li>\n<!-- /wp:list-item -->"
                    for i in items if i.strip())
    return f"<!-- wp:list -->\n<ul class=\"wp-block-list\">{lis}</ul>\n<!-- /wp:list -->"


def quote(text: str, attribution: str = "") -> str:
    cite = f"<cite>{sanitize_inline(attribution)}</cite>" if attribution.strip() else ""
    return ("<!-- wp:quote -->\n<blockquote class=\"wp-block-quote\"><!-- wp:paragraph -->\n"
            f"<p>{sanitize_inline(text)}</p>\n<!-- /wp:paragraph -->{cite}</blockquote>\n"
            "<!-- /wp:quote -->")


def image(media_id: int, src: str, alt: str, caption: str) -> str:
    return (f'<!-- wp:image {{"id":{int(media_id)},"sizeSlug":"large","linkDestination":"none"}} -->\n'
            f'<figure class="wp-block-image size-large"><img src="{html.escape(src, quote=True)}" '
            f'alt="{html.escape(alt, quote=True)}" class="wp-image-{int(media_id)}"/>'
            f'<figcaption class="wp-element-caption">{html.escape(caption)}</figcaption></figure>\n'
            "<!-- /wp:image -->")


def render_body(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        kind = (b.get("type") or "paragraph").lower()
        text = b.get("text") or ""
        if kind == "heading" and text.strip():
            out.append(heading(text))
        elif kind == "list":
            items = [i for i in (b.get("items") or []) if i and i.strip()]
            if items:
                if text.strip():
                    out.append(paragraph(text))
                out.append(bullet_list(items))
        elif kind == "quote" and text.strip():
            out.append(quote(text, b.get("attribution") or ""))
        elif text.strip():
            out.append(paragraph(text))
    return "\n\n".join(out)


def source_line(sources: list[dict]) -> str:
    """Final italic 'Source: … / Study: …' paragraph, per house style."""
    primary = [s for s in sources if s.get("role") in ("primary", "secondary", "press_release")]
    studies = [s for s in sources if s.get("role") == "study"]
    parts = []

    def link(s):
        label = s.get("publisher") or s.get("title") or urlparse(s.get("url", "")).netloc
        return (f'<a href="{html.escape(s["url"], quote=True)}">{html.escape(label)}</a>'
                if s.get("url") else html.escape(label))

    if primary:
        parts.append("Source: " + ", ".join(link(s) for s in primary[:3]))
    if studies:
        parts.append("Study: " + ", ".join(link(s) for s in studies[:2]))
    return paragraph(" / ".join(parts), italic=True) if parts else ""


def blocks_plain_text(blocks: list[dict]) -> str:
    lines = []
    for b in blocks:
        if b.get("text"):
            prefix = "## " if b.get("type") == "heading" else ""
            lines.append(prefix + plain_text(b["text"]))
        for i in b.get("items") or []:
            lines.append("- " + plain_text(i))
        if b.get("type") == "quote" and b.get("attribution"):
            lines.append("  — " + plain_text(b["attribution"]))
    return "\n\n".join(lines)


def links_in(blocks: list[dict]) -> list[str]:
    found = []
    for b in blocks:
        for chunk in [b.get("text") or "", *(b.get("items") or [])]:
            found += re.findall(r'href="(https?://[^"]+)"', sanitize_inline(chunk))
    return found
