"""Story discovery: Bing News RSS searches plus ordinary RSS/Atom feeds.

Produces `Item`s, filters out stale and off-topic entries, and groups items
covering the same story into `Cluster`s."""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse

log = logging.getLogger(__name__)

BING_RSS = "https://www.bing.com/news/search?q={q}&format=rss&setlang=en-US&cc=US"

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ocid|cmpid|taid|cvid|ei|smid|ref|src$)")

STOPWORDS = set(
    """a an and are as at be by for from has have he her his in into is it its of on or
    that the their this to was were will with after over new says said study shows finds
    could may more than about up out off how why what when who where shark sharks can you
    but not just now here reveal reveals report reports scientists researchers""".split()
)


@dataclass
class Item:
    title: str
    url: str
    summary: str = ""
    published: datetime | None = None
    publisher: str = ""
    feed: str = ""

    @property
    def domain(self) -> str:
        host = urlparse(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host


@dataclass
class Cluster:
    items: list[Item] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.items[0].title

    @property
    def newest(self) -> datetime | None:
        dates = [i.published for i in self.items if i.published]
        return max(dates) if dates else None

    @property
    def urls(self) -> list[str]:
        return [i.url for i in self.items]


# ---------------------------------------------------------------- parsing

def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def parse_feed(xml_text: str, feed_name: str = "") -> list[Item]:
    """Parse RSS 2.0 or Atom without third-party libraries."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as exc:
        log.warning("Could not parse feed %s: %s", feed_name, exc)
        return []

    items: list[Item] = []
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    for entry in entries:
        title = strip_html(_text(_child(entry, "title")))
        link_el = _child(entry, "link")
        url = ""
        if link_el is not None:
            url = (link_el.get("href") or _text(link_el)).strip()
        if not url:
            # Note: ElementTree elements without children are falsy, so no `or` chains.
            guid = _child(entry, "guid")
            if guid is None:
                guid = _child(entry, "id")
            url = _text(guid)
        summary = strip_html(
            _text(_child(entry, "description"))
            or _text(_child(entry, "summary"))
            or _text(_child(entry, "content"))
        )
        date = parse_date(
            _text(_child(entry, "pubdate"))
            or _text(_child(entry, "published"))
            or _text(_child(entry, "updated"))
            or _text(_child(entry, "date"))
        )
        publisher = _text(_child(entry, "source"))
        if not publisher:
            # Bing puts the outlet in <News:Source>
            for c in entry:
                if _local(c.tag) == "source":
                    publisher = _text(c)
        if not title or not url.startswith("http"):
            continue
        url = unwrap_redirect(url)
        items.append(Item(title=title, url=canonical_url(url), summary=summary[:600],
                          published=date, publisher=publisher, feed=feed_name))
    return items


def unwrap_redirect(url: str) -> str:
    """Bing News wraps outlet links in apiclick.aspx?url=<real url>."""
    parsed = urlparse(url)
    if "bing.com" in parsed.netloc and "apiclick" in parsed.path:
        real = parse_qs(parsed.query).get("url", [""])[0]
        if real.startswith("http"):
            return real
    return url


def canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = [(k, v) for k, vals in parse_qs(parsed.query, keep_blank_values=False).items()
             for v in vals if not TRACKING_PARAMS.match(k.lower())]
    scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, parsed.netloc.lower(), path, "", urlencode(query), ""))


# ---------------------------------------------------------------- filtering

def keyword_match(item: Item, keywords: Iterable[str]) -> bool:
    text = f"{item.title} {item.summary}".lower()
    for kw in keywords:
        if re.search(r"\b" + re.escape(kw.lower()) + r"s?\b", text):
            return True
    return False


def is_excluded(item: Item, patterns: Iterable[str]) -> bool:
    text = f"{item.title} {item.summary}"
    for pattern in patterns:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in text.lower():
                return True
    return False


def domain_in(domain: str, domains: Iterable[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


# ---------------------------------------------------------------- clustering

def _stem(word: str) -> str:
    """Tiny suffix stripper so 'hearing'/'hears'/'hear' and 'sounds'/'sound' match."""
    for suffix in ("ings", "ing", "ies", "ied", "ers", "es", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix in ("ies", "ied") else "")
    return word


def title_tokens(title: str) -> set[str]:
    text = re.sub(r"(\d+)([a-z]+)", r"\1 \2", title.lower().replace("’", "'"))  # 250ft -> 250 ft
    words = (w.strip("'") for w in re.findall(r"[a-z0-9']+", text))
    words = (w[:-2] if w.endswith("'s") else w for w in words)
    return {_stem(w) for w in words if w and w not in STOPWORDS and len(w) > 2}


def similarity(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def cluster_items(items: list[Item], threshold: float = 0.5) -> list[Cluster]:
    """Single-link clustering on title-token overlap: any two items whose titles
    overlap enough end up in the same cluster (connected components)."""
    ordered = sorted(items, key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tokens = [title_tokens(i.title) for i in ordered]
    for a in range(len(ordered)):
        for b in range(a + 1, len(ordered)):
            ta, tb = tokens[a], tokens[b]
            if ta and tb and len(ta & tb) / min(len(ta), len(tb)) >= threshold:
                parent[find(b)] = find(a)
    groups: dict[int, Cluster] = {}
    for idx, item in enumerate(ordered):
        groups.setdefault(find(idx), Cluster()).items.append(item)
    return list(groups.values())


# ---------------------------------------------------------------- discovery

def feed_urls(cfg_sources: dict) -> list[tuple[str, str, bool]]:
    """(name, url, require_keywords) for every source to poll."""
    out = [(f"Bing: {q}", BING_RSS.format(q=quote_plus(q)), True)
           for q in cfg_sources.get("bing_queries", [])]
    for feed in cfg_sources.get("feeds", []):
        out.append((feed.get("name", feed["url"]), feed["url"], bool(feed.get("require_keywords", True))))
    return out


def discover(cfg, http, now: datetime | None = None) -> list[Item]:
    sources = cfg["sources"]
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg["run"]["max_age_hours"])
    seen: dict[str, Item] = {}
    for name, url, require_kw in feed_urls(sources):
        try:
            resp = http.get(url, headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
        except Exception as exc:  # one bad feed shouldn't stop the run
            log.warning("Feed %s failed: %s", name, exc)
            continue
        if resp.status_code != 200:
            log.warning("Feed %s returned %s", name, resp.status_code)
            continue
        for item in parse_feed(resp.text, name):
            if item.published and item.published < cutoff:
                continue
            if item.published is None and name.startswith("Bing"):
                continue  # undated Bing items are usually stale evergreen pages
            if require_kw and not keyword_match(item, sources["keywords"]):
                continue
            if is_excluded(item, sources.get("exclude_patterns", [])):
                continue
            seen.setdefault(item.url, item)
    log.info("Discovered %d fresh, on-topic items", len(seen))
    return list(seen.values())
