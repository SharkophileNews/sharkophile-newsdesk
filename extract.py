"""Fetch an article page and pull out readable text, metadata and links to
likely primary sources (studies, press releases, agency pages)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from .discover import strip_html
from .http import USER_AGENT

log = logging.getLogger(__name__)

try:  # best extractor when available (installed in CI via requirements.txt)
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover - optional
    trafilatura = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - optional
    BeautifulSoup = None

PRIMARY_HINTS = (
    "doi.org", "eurekalert.org", "nature.com", "science.org", "sciencedirect.com",
    "pnas.org", "royalsocietypublishing.org", "frontiersin.org", "plos.org",
    "wiley.com", "springer.com", "cell.com", "biorxiv.org", "noaa.gov", "iucnredlist.org",
    "floridamuseum.ufl.edu", "fws.gov", ".gov/", ".edu/", "ocearch.org", "cites.org",
    "mdpi.com", "tandfonline.com", "academic.oup.com", "journals.",
)

MAX_TEXT_CHARS = 14000


@dataclass
class Article:
    url: str
    final_url: str = ""
    title: str = ""
    site_name: str = ""
    author: str = ""
    published: str = ""
    text: str = ""
    primary_links: list[str] = field(default_factory=list)
    blocked_reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.blocked_reason


class RobotsCache:
    def __init__(self, http):
        self.http = http
        self._cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._cache:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = self.http.get(base + "/robots.txt", timeout=10)
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                elif resp.status_code in (401, 403):
                    parser.disallow_all = True
                else:
                    parser = None  # no robots.txt -> allowed
            except Exception:
                parser = None
            self._cache[base] = parser
        parser = self._cache[base]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)


def _meta(html_text: str, *names: str) -> str:
    for name in names:
        pattern = (r'<meta[^>]+(?:property|name|itemprop)=["\']' + re.escape(name)
                   + r'["\'][^>]*content=["\']([^"\']+)')
        m = re.search(pattern, html_text, flags=re.IGNORECASE)
        if not m:
            pattern = (r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name|itemprop)=["\']'
                       + re.escape(name) + r'["\']')
            m = re.search(pattern, html_text, flags=re.IGNORECASE)
        if m:
            return strip_html(m.group(1))
    return ""


def _jsonld_article_body(html_text: str) -> str:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text,
                            flags=re.IGNORECASE | re.DOTALL):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("articleBody") and len(str(node["articleBody"])) > 400:
                    return strip_html(str(node["articleBody"]))
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    return ""


def _bs4_text(html_text: str) -> str:
    if BeautifulSoup is None:
        body = re.sub(r"(?is)<(script|style|nav|footer|header|aside|form)[^>]*>.*?</\1>", " ", html_text)
        paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)
        return "\n\n".join(t for t in (strip_html(p) for p in paras) if len(t) > 40)
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript",
                     "figure", "iframe", "button"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras = []
    for el in root.find_all(["p", "li", "h2", "h3", "blockquote"]):
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if el.name in ("h2", "h3"):
            if 3 < len(txt) < 120:
                paras.append(f"## {txt}")
        elif len(txt) > 40:
            paras.append(txt)
    # drop duplicates while keeping order
    seen, out = set(), []
    for p in paras:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return "\n\n".join(out)


def _primary_links(html_text: str, base_url: str) -> list[str]:
    links = []
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', html_text, flags=re.IGNORECASE):
        absolute = urljoin(base_url, href.strip())
        if not absolute.startswith("http"):
            continue
        low = absolute.lower()
        if urlparse(absolute).netloc == urlparse(base_url).netloc:
            continue
        if any(h in low for h in PRIMARY_HINTS) and absolute not in links:
            links.append(absolute)
    return links[:12]


def extract_from_html(html_text: str, url: str) -> Article:
    art = Article(url=url, final_url=url)
    art.title = _meta(html_text, "og:title", "twitter:title") or strip_html(
        (re.search(r"(?is)<title>(.*?)</title>", html_text) or [None, ""])[1])
    art.site_name = _meta(html_text, "og:site_name", "application-name")
    art.author = _meta(html_text, "author", "article:author", "parsely-author")
    art.published = _meta(html_text, "article:published_time", "datePublished", "pubdate",
                          "parsely-pub-date", "date")
    text = ""
    if trafilatura is not None:
        try:
            text = trafilatura.extract(html_text, url=url, include_comments=False,
                                       include_tables=False, favor_precision=True) or ""
        except Exception as exc:  # pragma: no cover
            log.debug("trafilatura failed on %s: %s", url, exc)
    if len(text) < 400:
        text = _jsonld_article_body(html_text) or text
    if len(text) < 400:
        text = _bs4_text(html_text)
    art.text = text[:MAX_TEXT_CHARS]
    art.primary_links = _primary_links(html_text, url)
    return art


def fetch_article(url: str, http, robots: RobotsCache | None = None) -> Article:
    if robots is not None and not robots.allowed(url):
        return Article(url=url, blocked_reason="robots.txt disallows fetching")
    try:
        resp = http.get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=25)
    except Exception as exc:
        return Article(url=url, blocked_reason=f"fetch failed: {exc}")
    if resp.status_code != 200:
        return Article(url=url, blocked_reason=f"HTTP {resp.status_code}")
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "xml" not in ctype and ctype:
        return Article(url=url, blocked_reason=f"not HTML ({ctype})")
    art = extract_from_html(resp.text, str(getattr(resp, "url", url) or url))
    art.url = url
    if len(art.text) < 300:
        art.blocked_reason = "too little readable text (paywall or script-rendered page)"
    return art
