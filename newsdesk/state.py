"""Run-to-run memory: which URLs we've seen and which stories we've covered.

Stored as JSON in state/newsdesk_state.json. In GitHub Actions the workflow
commits this file back to the repo after each run."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .discover import similarity

log = logging.getLogger(__name__)

SEEN_TTL_DAYS = 21
COVERED_TTL_DAYS = 45


def url_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


class State:
    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, str] = {}
        self.covered: list[dict] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("State file unreadable (%s); starting fresh", exc)
            return
        self.seen = data.get("seen", {})
        self.covered = data.get("covered", [])

    def save(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        seen_cut = (now - timedelta(days=SEEN_TTL_DAYS)).isoformat()
        cov_cut = (now - timedelta(days=COVERED_TTL_DAYS)).isoformat()
        self.seen = {k: v for k, v in self.seen.items() if v >= seen_cut}
        self.covered = [c for c in self.covered if c.get("date", "") >= cov_cut]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": self.seen, "covered": self.covered}, indent=1,
                                  sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------- queries
    def is_seen(self, url: str) -> bool:
        return url_key(url) in self.seen

    def mark_seen(self, urls, now: datetime | None = None) -> None:
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        for url in urls:
            self.seen[url_key(url)] = stamp

    def covered_match(self, title: str, urls: list[str], threshold: float = 0.6) -> dict | None:
        keys = {url_key(u) for u in urls}
        for story in self.covered:
            if keys & set(story.get("url_keys", [])):
                return story
            if similarity(title, story.get("title", "")) >= threshold:
                return story
        return None

    def add_covered(self, title: str, urls: list[str], post_id: int | None, headline: str,
                    now: datetime | None = None) -> None:
        self.covered.append({
            "title": title,
            "headline": headline,
            "url_keys": [url_key(u) for u in urls],
            "post_id": post_id,
            "date": (now or datetime.now(timezone.utc)).isoformat(),
        })

    def recent_headlines(self, limit: int = 40) -> list[str]:
        return [c.get("headline") or c.get("title", "") for c in self.covered[-limit:]]
