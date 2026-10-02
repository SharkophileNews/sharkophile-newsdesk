"""Deterministic SEO, style and integrity checks.

These run after the model writes a draft. Some problems are fixed in place
(slug format, meta description length); the rest are reported to the editor
and, for integrity problems, fed back to the writer for one revision."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .render import blocks_plain_text, links_in, plain_text

BANNED_PHRASES = [
    "delve", "dive into", "diving into", "majestic", "magnificent creature", "awe-inspiring",
    "captivating", "enigmatic", "testament to", "tapestry", "realm", "vast expanse",
    "in conclusion", "in summary", "it's important to note", "it is important to note",
    "buckle up", "game-changer", "game changer", "shark-infested", "shark infested",
    "man-eater", "man eater", "killer shark", "monster", "keywords:",
]

SLUG_STOPWORDS = set("a an and are as at be by for from in into is it its of on or the to with "
                     "new says after over".split())

SMALL_WORDS = set("a an and as at but by for from in into nor of off on or per so the to up via "
                  "vs with is are was be".split())


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "seo"  # seo | style | integrity


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", severity: str = "seo") -> None:
        self.checks.append(Check(name, ok, detail, severity))

    @property
    def score(self) -> int:
        weights = {"seo": 1.0, "style": 1.0, "integrity": 2.0}
        total = sum(weights[c.severity] for c in self.checks) or 1
        good = sum(weights[c.severity] for c in self.checks if c.ok)
        return round(100 * good / total)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def integrity_failures(self) -> list[Check]:
        return [c for c in self.failures if c.severity == "integrity"]

    def as_dict(self) -> dict:
        return {"score": self.score, "fixes": self.fixes,
                "checks": [c.__dict__ for c in self.checks]}


# ------------------------------------------------------------------ helpers

def slugify(text: str, max_words: int = 7) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", text.lower())
    keep = [w for w in words if w not in SLUG_STOPWORDS] or words
    return "-".join(keep[:max_words])


def trim_at_word(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:—-– ")
    return cut + "…"


def _syllables(word: str) -> int:
    word = word.lower()
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups)
    if word.endswith("e") and count > 1 and not word.endswith(("le", "ee")):
        count -= 1
    return max(1, count)


def reading_grade(text: str) -> float:
    sentences = max(1, len(re.findall(r"[.!?]+(?:\s|$)", text)))
    words = re.findall(r"[A-Za-z']+", text)
    if not words:
        return 0.0
    syll = sum(_syllables(w) for w in words)
    return round(0.39 * (len(words) / sentences) + 11.8 * (syll / len(words)) - 15.59, 1)


def looks_title_case(headline: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", headline)[1:]
    candidates = [w for w in words if w.lower() not in SMALL_WORDS and len(w) > 3]
    if len(candidates) < 3:
        return False
    caps = sum(1 for w in candidates if w[0].isupper())
    return caps / len(candidates) > 0.75


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


QUOTE_RE = re.compile(r"[\"“]([^\"”]{25,600})[\"”]")


def quotes_in(text: str) -> list[str]:
    return [m.group(1).strip() for m in QUOTE_RE.finditer(text)]


def unverified_quotes(draft_text: str, source_text: str) -> list[str]:
    """Quotes in the draft that don't appear word-for-word in any source."""
    src = _norm(source_text)
    missing = []
    for q in quotes_in(draft_text):
        nq = _norm(q).strip(" .,")
        # allow a trailing comma/period swap and leading/trailing ellipses
        core = nq.strip(".… ")
        if core and core not in src:
            missing.append(q)
    return missing


def copied_passages(draft_text: str, source_text: str, n: int = 12) -> list[str]:
    """Runs of n+ consecutive words copied from a source outside of quotation marks."""
    unquoted = QUOTE_RE.sub(" ", draft_text)
    words = re.findall(r"[a-z0-9']+", _norm(unquoted))
    src_words = re.findall(r"[a-z0-9']+", _norm(QUOTE_RE.sub(" ", source_text)))
    if len(words) < n or len(src_words) < n:
        return []
    shingles = {" ".join(src_words[i:i + n]) for i in range(len(src_words) - n + 1)}
    hits, i = [], 0
    while i <= len(words) - n:
        chunk = " ".join(words[i:i + n])
        if chunk in shingles:
            hits.append(chunk)
            i += n
        else:
            i += 1
    return hits


# ------------------------------------------------------------------ main

def check_and_fix(draft: dict, source_text: str, site_host: str,
                  internal_link_available: bool) -> Report:
    """Mutates `draft` with safe fixes and returns a report."""
    r = Report()
    kw = (draft.get("focus_keyword") or "").strip().lower()
    draft["focus_keyword"] = kw

    # --- headline
    headline = re.sub(r"\s+", " ", draft.get("headline", "")).strip().rstrip(".")
    draft["headline"] = headline
    r.add("Headline length ≤ 70", len(headline) <= 70, f"{len(headline)} chars", "style")
    r.add("Headline in sentence case", not looks_title_case(headline), headline, "style")

    # --- SEO title
    seo_title = re.sub(r"\s+", " ", draft.get("seo_title") or headline).strip()
    if len(seo_title) > 60:
        seo_title = trim_at_word(seo_title, 60).rstrip("…")
        r.fixes.append("Trimmed SEO title to 60 characters")
    draft["seo_title"] = seo_title
    r.add("SEO title ≤ 60 chars", len(seo_title) <= 60, f"{len(seo_title)} chars")
    r.add("Focus keyword in SEO title", bool(kw) and _kw_in(kw, seo_title), seo_title)

    # --- slug
    slug = slugify(draft.get("slug") or seo_title)
    if slug != draft.get("slug"):
        r.fixes.append(f"Normalized slug to '{slug}'")
    draft["slug"] = slug
    r.add("Slug 3–7 words", 3 <= len(slug.split("-")) <= 7, slug)
    r.add("Focus keyword in slug", bool(kw) and all(w in slug.split("-")
          for w in slugify(kw, 10).split("-") if w), slug)

    # --- meta description
    meta = re.sub(r"\s+", " ", plain_text(draft.get("meta_description", ""))).strip()
    if len(meta) > 155:
        meta = trim_at_word(meta, 155)
        r.fixes.append("Trimmed meta description to 155 characters")
    draft["meta_description"] = meta
    r.add("Meta description 120–155 chars", 120 <= len(meta) <= 155, f"{len(meta)} chars")
    r.add("Focus keyword in meta description", bool(kw) and _kw_in(kw, meta))

    # --- body
    blocks = draft.get("body") or []
    body_text = blocks_plain_text(blocks)
    words = len(re.findall(r"\b\w+\b", body_text))
    draft["_word_count"] = words
    paragraphs = [plain_text(b.get("text", "")) for b in blocks if b.get("type") == "paragraph"]
    first = paragraphs[0] if paragraphs else ""
    r.add("Length 250–900 words", 250 <= words <= 900, f"{words} words", "style")
    r.add("Focus keyword in first paragraph", bool(kw) and _kw_in(kw, first))
    long_paras = [p for p in paragraphs if len(p.split()) > 60]
    r.add("Paragraphs ≤ 60 words", not long_paras, f"{len(long_paras)} long paragraph(s)", "style")
    has_h2 = any(b.get("type") == "heading" for b in blocks)
    r.add("Subheads for longer stories", has_h2 or words <= 450,
          "add H2 subheads" if not has_h2 and words > 450 else "")
    grade = reading_grade(body_text)
    r.add("Reading grade ≤ 12", grade <= 12.0, f"grade {grade}", "style")

    lowered = body_text.lower() + " " + headline.lower()
    banned = sorted({p for p in BANNED_PHRASES if re.search(r"\b" + re.escape(p), lowered)})
    r.add("No banned phrases", not banned, ", ".join(banned), "style")

    links = links_in(blocks)
    outbound = [l for l in links if site_host not in urlparse(l).netloc]
    internal = [l for l in links if site_host in urlparse(l).netloc]
    r.add("Links to a primary source", bool(outbound), f"{len(outbound)} outbound link(s)")
    if internal_link_available:
        r.add("Internal link to related story", bool(internal), f"{len(internal)} internal link(s)")

    # --- image + taxonomy
    alt = (draft.get("image") or {}).get("alt_text", "").strip()
    if len(alt) > 125:
        draft["image"]["alt_text"] = trim_at_word(alt, 125).rstrip("…")
        r.fixes.append("Trimmed image alt text to 125 characters")
    r.add("Image alt text present", bool(alt))
    cats = draft.get("categories") or []
    r.add("News + 1–2 topical categories", "news" in cats and 2 <= len(cats) <= 3, ", ".join(cats))
    r.add("3–6 tags", 3 <= len(draft.get("tags") or []) <= 6, ", ".join(draft.get("tags") or []))

    # --- integrity (fed back to the writer if they fail)
    bad_quotes = unverified_quotes(body_text, source_text)
    r.add("Quotes match sources word-for-word", not bad_quotes,
          " | ".join(q[:90] for q in bad_quotes), "integrity")
    copied = copied_passages(body_text, source_text)
    r.add("No passages copied from sources", not copied,
          " | ".join(c[:90] for c in copied[:3]), "integrity")
    return r


def _kw_in(kw: str, text: str) -> bool:
    """Keyword present, tolerating plurals and word order within the phrase."""
    text_words = set(re.findall(r"[a-z0-9]+", text.lower()))
    kw_words = [w for w in re.findall(r"[a-z0-9]+", kw.lower()) if w not in SLUG_STOPWORDS]
    if not kw_words:
        return False
    return all(w in text_words or w + "s" in text_words or w.rstrip("s") in text_words
               for w in kw_words)
