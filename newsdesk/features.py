"""Planned features: evergreen guides, SharkoFiles species profiles, legends and
pop culture, and seasonal pieces, listed by date in content/calendar.yaml.

Each entry is drafted `features.lead_days` days before its date (the daily run
calls `python -m newsdesk features`), researched on the web, written on the
feature model, fact-checked, illustrated and filed as a WordPress draft like the
news. Nothing is published automatically.

    python -m newsdesk features                    # draft whatever is due
    python -m newsdesk features --date 2026-10-08  # draft that entry now
    python -m newsdesk features --list             # show the calendar"""

from __future__ import annotations

import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

from . import prompts, seo
from .discover import Cluster, Item, domain_in
from .extract import fetch_article
from .pipeline import Newsdesk, Story, filter_sources, known_urls, source_bundle, today_str

log = logging.getLogger(__name__)

PILLARS = {"guide", "sharkofiles", "legends", "pop-culture", "seasonal"}


@dataclass
class Entry:
    date: date
    pillar: str
    title: str
    brief: str
    focus_keyword: str = ""
    related: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    hub: bool = False
    newsdesk: bool = True

    @property
    def key(self) -> str:
        return self.date.isoformat()

    def planned_for(self) -> str:
        return f"{self.date:%A, %B} {self.date.day}, {self.date.year}"


class CalendarError(ValueError):
    pass


def load_calendar(path: Path) -> list[Entry]:
    if not path.exists():
        raise CalendarError(f"Content calendar not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = raw.get("features", raw) if isinstance(raw, dict) else raw
    entries, seen = [], set()
    for i, row in enumerate(rows or []):
        try:
            d = row["date"] if isinstance(row["date"], date) else date.fromisoformat(str(row["date"]))
            e = Entry(date=d, pillar=str(row["pillar"]).strip().lower(), title=str(row["title"]).strip(),
                      brief=re.sub(r"\s+", " ", str(row["brief"])).strip(),
                      focus_keyword=str(row.get("focus_keyword") or "").strip(),
                      related=[str(x) for x in row.get("related") or []],
                      categories=[str(x).lower() for x in row.get("categories") or []],
                      hub=bool(row.get("hub", False)), newsdesk=bool(row.get("newsdesk", True)))
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarError(f"Calendar entry {i + 1} is incomplete or malformed: {exc}") from exc
        if e.pillar not in PILLARS:
            raise CalendarError(f"Calendar entry {e.key}: unknown pillar '{e.pillar}' "
                                f"(use one of {', '.join(sorted(PILLARS))})")
        if e.key in seen:
            raise CalendarError(f"Calendar has two entries for {e.key}")
        seen.add(e.key)
        entries.append(e)
    return sorted(entries, key=lambda e: e.date)


def due_entries(entries: list[Entry], today: date, lead_days: int, done) -> list[Entry]:
    """Entries dated today through today + lead_days that haven't been drafted yet."""
    last = today + timedelta(days=lead_days)
    return [e for e in entries if e.newsdesk and today <= e.date <= last and not done(e.key)]


def normalize_feature_taxonomy(draft: dict, entry: Entry, cfg) -> None:
    wpcfg = cfg["wordpress"]
    allowed = [c.lower() for c in wpcfg["allowed_categories"]]
    cats = [c for c in entry.categories if c in allowed]
    cats += [c.strip().lower() for c in draft.get("categories") or [] if c.strip().lower() in allowed]
    cats = list(dict.fromkeys(cats))[: wpcfg["max_categories"]]
    draft["categories"] = cats or [wpcfg.get("default_category", "news")]
    tags = []
    for t in draft.get("tags") or []:
        t = re.sub(r"^#", "", t).strip()
        if t and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    draft["tags"] = tags[: wpcfg["max_tags"]]


class FeatureDesk(Newsdesk):
    """Reuses the news desk's WordPress, fact-check, image and publishing steps."""

    @property
    def fcfg(self) -> dict:
        return self.cfg["features"]

    def calendar(self) -> list[Entry]:
        return load_calendar(self.cfg.root / self.fcfg["calendar"])

    def local_today(self) -> date:
        return self.now.astimezone(ZoneInfo(self.cfg["site"]["timezone"])).date()

    # ------------------------------------------------------------- stages
    def related_for(self, entry: Entry) -> list[dict]:
        terms = entry.related or [entry.focus_keyword or entry.title]
        per_term = 2 if entry.hub else 3
        found: dict[int, dict] = {}
        for term in terms[:8]:
            try:
                for p in self.wp.search_posts(term, limit=per_term):
                    found.setdefault(p["id"], p)
            except Exception as exc:
                log.debug("Related search failed for %s: %s", term, exc)
        return list(found.values())[:8]

    def gather(self, entry: Entry, cites: list[dict], related: list[dict]) -> Story:
        src = self.cfg["sources"]
        blocked = src.get("blocked_domains", [])
        preferred = src.get("preferred_domains", [])

        def rank(c: dict):
            host = urlparse(c["url"]).netloc.lower().removeprefix("www.")
            return (not domain_in(host, preferred), -len(c.get("cited_text") or ""))

        candidates = [c for c in cites if c.get("url", "").startswith("http")
                      and not domain_in(urlparse(c["url"]).netloc.lower().removeprefix("www."), blocked)
                      and self.site_host not in urlparse(c["url"]).netloc]
        got = []
        for c in sorted(candidates, key=rank):
            if len(got) >= int(self.fcfg["max_sources"]):
                break
            art = fetch_article(c["url"], self.http, self.robots)
            if art.ok:
                art.title = art.title or c.get("title", "")
                art.site_name = art.site_name or urlparse(c["url"]).netloc.removeprefix("www.")
                got.append(art)
            else:
                log.info("Skipping research page %s: %s", c["url"], art.blocked_reason)
        if entry.hub:
            # A hub summarizes Sharkophile's own stories, so read them too.
            for p in related[:6]:
                art = fetch_article(p["link"], self.http, self.robots)
                if art.ok:
                    art.title = art.title or p["title"]
                    art.site_name = art.site_name or "Sharkophile"
                    got.append(art)
        items = [Item(title=entry.title, url=a.url, published=self.now, publisher=a.site_name) for a in got]
        story = Story(cluster=Cluster(items=items or [Item(title=entry.title, url=self.cfg.site_url,
                                                           published=self.now)]),
                      triage={"story_type": "feature", "pillar": entry.pillar, "score": None},
                      articles=got)
        story.sources = [{
            "id": f"S{i + 1}", "title": a.title, "publisher": a.site_name, "url": a.url,
            "published": a.published, "text": a.text, "primary_links": a.primary_links,
        } for i, a in enumerate(got)]
        return story

    def write_feature(self, entry: Entry) -> tuple[Story, dict]:
        cfg = self.cfg
        notes, cites = self.claude.research(
            cfg["models"]["fact_check"], prompts.FEATURE_RESEARCH_SYSTEM,
            prompts.feature_research_user(entry.title, entry.brief),
            max_searches=int(self.fcfg["research_max_searches"]), max_tokens=8000)
        related = self.related_for(entry)
        story = self.gather(entry, cites, related)
        material = sum(len(s["text"]) for s in story.sources) + len(notes)
        if material < int(self.fcfg["min_material_chars"]):
            raise RuntimeError(f"not enough research material ({material} characters); "
                               "add sources to the brief or write this one by hand")
        try:
            tag_names = self.wp.popular_tag_names()
        except Exception:
            tag_names = []
        user = prompts.feature_writer_user(
            today=today_str(cfg, self.now), planned_for=entry.planned_for(), pillar=entry.pillar,
            title=entry.title, brief=entry.brief, focus_keyword=entry.focus_keyword, hub=entry.hub,
            sources=story.sources, research_notes=notes, research_citations=cites,
            related_posts=related, existing_tags=tag_names,
            allowed_categories=cfg["wordpress"]["allowed_categories"],
            suggested_categories=entry.categories)
        system = prompts.feature_writer_system(cfg.style_guide)
        model = cfg["models"].get("feature_writer") or cfg["models"]["writer"]
        draft = self.claude.json(model, system, user, prompts.DRAFT_SCHEMA, max_tokens=24000)
        bundle = source_bundle(story, notes)
        allowed = known_urls(story, cites, related)
        all_source_text = bundle + "\n" + "\n".join(c.get("cited_text", "") for c in cites)

        def evaluate(d: dict):
            normalize_feature_taxonomy(d, entry, cfg)
            unknown = filter_sources(d, allowed)
            rep = seo.check_and_fix(d, all_source_text, self.site_host, bool(related), kind="feature")
            if unknown:
                rep.add("Links come from the source material", False, ", ".join(unknown[:3]), "integrity")
            return rep, self.fact_check(d, bundle)

        report, fact = evaluate(draft)
        revised = False
        problems = [f"{c.name}: {c.detail}" for c in report.integrity_failures]
        problems += [f"{i['problem']} (draft text: “{i['excerpt']}”; fix: {i['fix']})"
                     for i in fact.get("issues", []) if i.get("severity") == "major"]
        if problems and cfg["run"].get("auto_revise"):
            log.info("Revising feature to fix %d problem(s)", len(problems))
            draft = self.claude.json(model, system, user + "\n\n" + prompts.revise_user(draft, problems),
                                     prompts.DRAFT_SCHEMA, max_tokens=24000)
            report, fact = evaluate(draft)
            revised = True
        draft.update(_report=report, _fact=fact, _revised=revised, _related=related,
                     _research={"notes": notes, "citations": cites},
                     _extra_warnings=[f"Planned feature for {entry.planned_for()} — "
                                      "publish or schedule it for that morning"])
        return story, draft

    # ------------------------------------------------------------- run
    def run_features(self, dates: list[str] | None = None) -> dict:
        summary: dict = {"run_id": self.run_id, "dry_run": self.dry_run, "kind": "features",
                         "drafts": [], "errors": [], "considered": 0}
        try:
            entries = self.calendar()
            if dates:
                wanted = set(dates)
                chosen = [e for e in entries if e.key in wanted]
                for d in sorted(wanted - {e.key for e in chosen}):
                    summary["errors"].append(f"No calendar entry dated {d}")
            else:
                chosen = due_entries(entries, self.local_today(), int(self.fcfg["lead_days"]),
                                     self.state.feature_done)
            summary["considered"] = len(chosen)
            if not chosen:
                log.info("No planned features due")
                return self.finish(summary)
            self.preflight()
            for entry in chosen:
                try:
                    story, draft = self.write_feature(entry)
                    result = self.publish(story, draft)
                    result["planned_for"] = entry.key
                    summary["drafts"].append(result)
                    if not self.dry_run:
                        self.state.mark_feature(entry.key, result.get("post_id"), result["headline"],
                                                self.now)
                except Exception as exc:
                    log.error("Feature failed: %s\n%s", entry.title, traceback.format_exc())
                    summary["errors"].append(f"{entry.title}: {exc}")
        except Exception as exc:
            log.error("Features run failed: %s\n%s", exc, traceback.format_exc())
            summary["fatal"] = str(exc)
        return self.finish(summary)
