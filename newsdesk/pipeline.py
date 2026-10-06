"""The newsdesk run: discover → triage → research → write → check → illustrate
→ draft in WordPress → alert the editor."""

from __future__ import annotations

import html
import json
import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import images, notify, prompts, render, seo
from .config import Config
from .discover import Cluster, Item, cluster_items, discover, domain_in
from .extract import Article, RobotsCache, fetch_article
from .http import HttpClient
from .llm import Claude
from .state import State, url_key
from .wordpress import WordPress, seo_meta

log = logging.getLogger(__name__)

STORY_TYPE_CATEGORY = {
    "science": "science", "conservation": "conservation", "attack": "attacks",
    "entertainment": "entertainment", "events": "events", "fishing": "fishing",
    "diving": "diving", "odd": "odd", "news": "news",
}

PRESS_RELEASE_HINTS = ("eurekalert.org", ".edu", ".gov", "ocearch.org", "floridamuseum.ufl.edu",
                       "phys.org", "sciencedaily.com", "theconversation.com")


@dataclass
class Story:
    cluster: Cluster
    triage: dict
    articles: list[Article] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)


# ====================================================================== helpers

def today_str(cfg: Config, now: datetime) -> str:
    local = now.astimezone(ZoneInfo(cfg["site"]["timezone"]))
    return f"{local:%A, %B} {local.day}, {local.year}"


def candidate_rows(clusters: list[Cluster]) -> list[dict]:
    rows = []
    for i, c in enumerate(clusters):
        rows.append({
            "index": i,
            "title": c.title,
            "outlets": sorted({it.publisher or it.domain for it in c.items})[:6],
            "published": c.newest.isoformat() if c.newest else "",
            "summary": max((it.summary for it in c.items), key=len)[:400],
            "coverage_count": len(c.items),
        })
    return rows


def select_stories(clusters: list[Cluster], triage_rows: list[dict], cfg: Config,
                   max_n: int, force_all: bool = False) -> list[Story]:
    by_index = {r["index"]: r for r in triage_rows if 0 <= r.get("index", -1) < len(clusters)}
    # fold duplicates into the earlier cluster
    for idx, row in by_index.items():
        target = row.get("same_story_as", -1)
        if isinstance(target, int) and 0 <= target < len(clusters) and target != idx:
            clusters[target].items.extend(clusters[idx].items)
    runcfg = cfg["run"]
    eligible = []
    for idx, row in by_index.items():
        if force_all:
            eligible.append((idx, row))
            continue
        if not row.get("is_shark_story") or row.get("already_covered"):
            continue
        if isinstance(row.get("same_story_as"), int) and row["same_story_as"] not in (-1, idx):
            continue
        if int(row.get("score", 0)) < runcfg["min_score"]:
            continue
        eligible.append((idx, row))
    eligible.sort(key=lambda x: (int(x[1].get("score", 0)),
                                 clusters[x[0]].newest or datetime.min.replace(tzinfo=timezone.utc),
                                 len(clusters[x[0]].items)), reverse=True)
    picked, used_types = [], set()
    for idx, row in eligible:
        if len(picked) >= max_n:
            break
        stype = (row.get("story_type") or "news").lower()
        if (not force_all and stype in used_types
                and int(row.get("score", 0)) < runcfg["diversity_override_score"]):
            continue
        used_types.add(stype)
        picked.append(Story(cluster=clusters[idx], triage=row))
    return picked


def rank_items(items: list[Item], cfg: Config) -> list[Item]:
    src = cfg["sources"]

    def key(it: Item):
        blocked = domain_in(it.domain, src.get("blocked_domains", []))
        preferred = domain_in(it.domain, src.get("preferred_domains", []))
        return (blocked, not preferred, -len(it.summary))

    return sorted(items, key=key)


def gather_sources(story: Story, cfg: Config, http, robots: RobotsCache) -> None:
    """Fetch up to three readable articles for the story, plus one press release if linked."""
    blocked = cfg["sources"].get("blocked_domains", [])
    got: list[Article] = []
    tried: set[str] = set()
    for item in rank_items(story.cluster.items, cfg):
        if len(got) >= 3:
            break
        if domain_in(item.domain, blocked) or item.url in tried:
            continue
        tried.add(item.url)
        art = fetch_article(item.url, http, robots)
        if not art.title:
            art.title = item.title
        if not art.site_name:
            art.site_name = item.publisher or item.domain
        if not art.published and item.published:
            art.published = item.published.isoformat()
        if art.ok:
            got.append(art)
        else:
            log.info("Skipping source %s: %s", item.url, art.blocked_reason)
    # follow one press-release-style primary link if none of our sources is one
    if got and not any(any(h in urlparse(a.url).netloc for h in PRESS_RELEASE_HINTS) for a in got):
        for link in got[0].primary_links:
            host = urlparse(link).netloc
            if link in tried or not any(h in host for h in PRESS_RELEASE_HINTS):
                continue
            tried.add(link)
            art = fetch_article(link, http, robots)
            if art.ok:
                art.site_name = art.site_name or host
                got.append(art)
                break
    story.articles = got
    story.sources = [{
        "id": f"S{i + 1}", "title": a.title, "publisher": a.site_name, "url": a.url,
        "published": a.published, "text": a.text, "primary_links": a.primary_links,
    } for i, a in enumerate(got)]


def source_bundle(story: Story, research_notes: str = "") -> str:
    parts = [f"[{s['id']}] {s['title']} ({s['publisher']}) {s['url']}\n{s['text']}" for s in story.sources]
    if research_notes:
        parts.append(f"[R] Web research notes\n{research_notes}")
    return "\n\n".join(parts)


def known_urls(story: Story, research_cites: list[dict], related: list[dict]) -> set[str]:
    urls = set()
    for s in story.sources:
        urls.add(s["url"])
        urls.update(s.get("primary_links", []))
    urls.update(c["url"] for c in research_cites)
    urls.update(p["link"] for p in related)
    urls.update(story.cluster.urls)
    return {u.rstrip("/") for u in urls}


def normalize_taxonomy(draft: dict, story: Story, cfg: Config) -> None:
    wpcfg = cfg["wordpress"]
    allowed = [c.lower() for c in wpcfg["allowed_categories"]]
    cats = [c.strip().lower() for c in draft.get("categories") or []]
    cats = [c for c in cats if c in allowed]
    default = wpcfg.get("default_category", "news")
    if default not in cats:
        cats.insert(0, default)
    if len(cats) < 2:
        mapped = STORY_TYPE_CATEGORY.get((story.triage.get("story_type") or "").lower())
        if mapped and mapped in allowed and mapped not in cats:
            cats.append(mapped)
    draft["categories"] = list(dict.fromkeys(cats))[: wpcfg["max_categories"]]
    tags = []
    for t in draft.get("tags") or []:
        t = re.sub(r"^#", "", t).strip()
        if t and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    draft["tags"] = tags[: wpcfg["max_tags"]]


def filter_sources(draft: dict, allowed: set[str]) -> list[str]:
    """Drop listed sources whose URLs weren't in the material; return unknown body links."""
    draft["sources"] = [s for s in draft.get("sources") or []
                        if s.get("url", "").rstrip("/") in allowed]
    body_links = render.links_in(draft.get("body") or [])
    return [l for l in body_links if l.rstrip("/") not in allowed]


def warnings_for(report: seo.Report, fact: dict, draft: dict, extra: list[str]) -> list[str]:
    out = list(extra)
    if fact.get("verdict") == "major_issues":
        out.append("Fact-check found MAJOR issues — verify before publishing")
    for issue in fact.get("issues", []):
        if issue.get("severity") == "major":
            out.append(f"Fact-check: {issue.get('problem','')} — “{issue.get('excerpt','')[:80]}”")
    for c in report.integrity_failures:
        out.append(f"{c.name}: {c.detail}")
    if draft.get("confidence") == "low":
        out.append("Writer confidence is LOW")
    seo_fails = [c.name for c in report.failures if c.severity != "integrity"]
    if seo_fails:
        out.append("Checks to review: " + "; ".join(seo_fails[:6]))
    return out


def preview_html(draft: dict, content_html: str, image_file: str | None) -> str:
    img = f"<img src='{html.escape(image_file)}' style='max-width:100%'>" if image_file else ""
    return (
        "<!doctype html><meta charset='utf-8'><title>" + html.escape(draft["headline"]) + "</title>"
        "<style>body{font:17px/1.6 Georgia,serif;max-width:760px;margin:40px auto;padding:0 16px;color:#222}"
        "h1{font-family:system-ui,sans-serif}.meta{font:13px system-ui,sans-serif;color:#666;"
        "background:#f5f5f5;padding:10px;border-radius:6px}</style>"
        f"<div class='meta'>SEO title: {html.escape(draft['seo_title'])}<br>Slug: {html.escape(draft['slug'])}"
        f"<br>Meta: {html.escape(draft['meta_description'])}<br>Focus: {html.escape(draft['focus_keyword'])}"
        f"<br>Categories: {html.escape(', '.join(draft['categories']))} · Tags: {html.escape(', '.join(draft['tags']))}</div>"
        f"<h1>{html.escape(draft['headline'])}</h1>{img}{content_html}"
    )


# ====================================================================== core

class Newsdesk:
    def __init__(self, cfg: Config, http=None, dry_run: bool = False, now: datetime | None = None):
        self.cfg = cfg
        self.http = http or HttpClient()
        self.dry_run = dry_run
        self.now = now or datetime.now(timezone.utc)
        self.run_id = self.now.strftime("%Y%m%d-%H%M%S")
        self.state = State(cfg.state_path)
        self.claude = Claude(cfg.secrets.anthropic_api_key, self.http)
        self.wp = WordPress(cfg.site_url, cfg.secrets.wp_user, cfg.secrets.wp_app_password, self.http)
        self.robots = RobotsCache(self.http)
        self.registered_meta: set[str] | None = None
        self.site_host = urlparse(cfg.site_url).netloc.replace("www.", "")
        self.out = cfg.out_dir / self.run_id
        self.out.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- stages
    def preflight(self) -> None:
        if not self.cfg.secrets.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        if self.dry_run:
            try:
                self.registered_meta = self.wp.registered_post_meta() if self.wp.auth_header else None
            except Exception:
                self.registered_meta = None
            return
        me = self.wp.whoami()
        caps = me.get("capabilities", {})
        for cap in ("edit_posts", "upload_files"):
            if caps and not caps.get(cap):
                raise RuntimeError(f"WordPress user '{me.get('slug')}' lacks the {cap} capability")
        self.registered_meta = self.wp.registered_post_meta()

    def triage(self, clusters: list[Cluster]) -> list[dict]:
        recent = self.state.recent_headlines()
        try:
            recent = list(dict.fromkeys(recent + self.wp.recent_titles(30)))[-80:]
        except Exception as exc:
            log.warning("Could not load recent WordPress titles: %s", exc)
        rows = candidate_rows(clusters)
        result = self.claude.json(self.cfg["models"]["triage"], prompts.TRIAGE_SYSTEM,
                                  prompts.triage_user(rows, recent, today_str(self.cfg, self.now)),
                                  prompts.TRIAGE_SCHEMA, max_tokens=12000)
        return result.get("stories", [])

    def research(self, story: Story) -> tuple[str, list[dict]]:
        rc = self.cfg["run"]
        if not rc.get("web_research"):
            return "", []
        types = [t.lower() for t in (rc.get("web_research_types") or [])]
        stype = (story.triage.get("story_type") or "news").lower()
        if types and stype not in types:
            log.info("No web research for a %s story (web_research_types: %s)", stype, ", ".join(types))
            return "", []
        try:
            return self.claude.research(
                self.cfg["models"]["fact_check"], prompts.RESEARCH_SYSTEM,
                prompts.research_user(story.cluster.title, story.cluster.urls,
                                      story.cluster.items[0].summary),
                max_searches=int(rc.get("web_research_max_searches", 3)))
        except Exception as exc:
            log.warning("Web research failed (continuing without it): %s", exc)
            return "", []

    def related_posts(self, story: Story) -> list[dict]:
        found: dict[int, dict] = {}
        terms = list(story.triage.get("search_terms") or [])
        if story.triage.get("focus_species"):
            terms.insert(0, story.triage["focus_species"])
        for term in terms[:3]:
            try:
                for p in self.wp.search_posts(term, limit=3):
                    found.setdefault(p["id"], p)
            except Exception as exc:
                log.debug("Related search failed for %s: %s", term, exc)
        return list(found.values())[:5]

    def fact_check(self, draft: dict, bundle: str) -> dict:
        text = render.blocks_plain_text(draft["body"])
        try:
            return self.claude.json(
                self.cfg["models"]["fact_check"], prompts.FACT_CHECK_SYSTEM + "\n\n=== HOUSE STYLE ===\n"
                + self.cfg.style_guide, prompts.fact_check_user(
                    text, draft["headline"], draft["meta_description"],
                    bundle, render.links_in(draft["body"])),
                prompts.FACT_CHECK_SCHEMA, max_tokens=8000)
        except Exception as exc:
            log.warning("Fact-check failed: %s", exc)
            return {"verdict": "not_run", "issues": [], "summary": f"Fact-check failed to run: {exc}"}

    def write(self, story: Story) -> dict:
        cfg = self.cfg
        notes, cites = self.research(story)
        related = self.related_posts(story)
        try:
            tag_names = self.wp.popular_tag_names()
        except Exception:
            tag_names = []
        user = prompts.writer_user(
            today=today_str(cfg, self.now), triage=story.triage, sources=story.sources,
            research_notes=notes, research_citations=cites, related_posts=related,
            existing_tags=tag_names, allowed_categories=cfg["wordpress"]["allowed_categories"])
        system = prompts.writer_system(cfg.style_guide)
        draft = self.claude.json(cfg["models"]["writer"], system, user, prompts.DRAFT_SCHEMA,
                                 max_tokens=20000)
        bundle = source_bundle(story, notes)
        allowed = known_urls(story, cites, related)
        all_source_text = bundle + "\n" + "\n".join(c.get("cited_text", "") for c in cites)

        def evaluate(d: dict):
            normalize_taxonomy(d, story, cfg)
            unknown = filter_sources(d, allowed)
            rep = seo.check_and_fix(d, all_source_text, self.site_host, bool(related))
            if unknown:
                rep.add("Links come from the source material", False, ", ".join(unknown[:3]), "integrity")
            fc = self.fact_check(d, bundle)
            return rep, fc

        report, fact = evaluate(draft)
        revised = False
        problems = [f"{c.name}: {c.detail}" for c in report.integrity_failures]
        problems += [f"{i['problem']} (draft text: “{i['excerpt']}”; fix: {i['fix']})"
                     for i in fact.get("issues", []) if i.get("severity") == "major"]
        if problems and cfg["run"].get("auto_revise"):
            log.info("Revising draft to fix %d problem(s)", len(problems))
            draft = self.claude.json(cfg["models"]["writer"], system,
                                     user + "\n\n" + prompts.revise_user(draft, problems),
                                     prompts.DRAFT_SCHEMA, max_tokens=20000)
            report, fact = evaluate(draft)
            revised = True
        draft["_report"] = report
        draft["_fact"] = fact
        draft["_revised"] = revised
        draft["_related"] = related
        draft["_research"] = {"notes": notes, "citations": cites}
        return draft

    def illustrate(self, draft: dict) -> tuple[images.ImageResult | None, str]:
        try:
            img = images.generate(self.cfg["images"], self.cfg.secrets.openai_api_key, self.http,
                                  draft["image"]["prompt"])
            return img, "" if img else "No featured image: image generation is switched off or OPENAI_API_KEY is missing"
        except Exception as exc:
            log.warning("Image generation failed: %s", exc)
            return None, f"No featured image — add one before publishing. {exc}"

    def publish(self, story: Story, draft: dict) -> dict:
        cfg, site = self.cfg, self.cfg["site"]
        report: seo.Report = draft["_report"]
        fact = draft["_fact"]
        extra_warnings: list[str] = list(draft.get("_extra_warnings", []))
        img, img_warning = self.illustrate(draft)
        if img_warning:
            extra_warnings.append(img_warning)

        slug = draft["slug"]
        media_id, media_url, image_file = None, "", None
        caption = cfg["images"]["caption"]
        if img:
            image_file = f"{slug}.{img.ext}"
            (self.out / image_file).write_bytes(img.data)
            if not self.dry_run:
                media = self.wp.upload_media(img.data, image_file, img.mime, draft["image"]["alt_text"],
                                             caption, draft["headline"], description=img.prompt)
                media_id = media["id"]
                media_url = (media.get("media_details", {}).get("sizes", {}).get("large", {})
                             .get("source_url") or media.get("source_url", ""))

        body_html = render.render_body(draft["body"])
        if not draft.get("sources"):
            # never publish without attribution: fall back to the articles we actually used
            draft["sources"] = [{"title": s["title"], "publisher": s["publisher"], "url": s["url"],
                                 "role": "primary"} for s in story.sources[:2]]
        src_line = render.source_line(draft.get("sources", []))
        parts = []
        if media_id and site.get("embed_image_in_content", True):
            parts.append(render.image(media_id, media_url, draft["image"]["alt_text"], caption))
        parts.append(body_html)
        if src_line:
            parts.append(src_line)
        content = "\n\n".join(parts)

        meta, skipped = seo_meta(site.get("seo_plugin", "genesis"), draft["seo_title"],
                                 draft["meta_description"], draft["focus_keyword"], self.registered_meta)
        if skipped and site.get("seo_plugin") not in (None, "none"):
            extra_warnings.append("SEO title/description fields not writable — install the "
                                  "Sharkophile Newsdesk Support plugin (excerpt was still set)")
        newsdesk_meta = {
            "jetpack_publicize_message": draft.get("social_message", "")[:250],
            "_newsdesk_sources": json.dumps(draft.get("sources", [])),
            "_newsdesk_focus_keyword": draft["focus_keyword"],
            "_newsdesk_run_id": self.run_id,
        }
        for k, v in newsdesk_meta.items():
            if self.registered_meta is None or k in self.registered_meta:
                meta[k] = v

        warnings = warnings_for(report, fact, draft, extra_warnings)
        result = {
            "headline": draft["headline"], "seo_title": draft["seo_title"], "slug": slug,
            "meta_description": draft["meta_description"], "focus_keyword": draft["focus_keyword"],
            "categories": draft["categories"], "tags": draft["tags"], "sources": draft.get("sources", []),
            "word_count": draft.get("_word_count", 0), "seo_score": report.score,
            "fact_check": {"verdict": fact.get("verdict"), "summary": fact.get("summary", ""),
                           "issues": fact.get("issues", [])},
            "revised": draft.get("_revised", False), "editor_notes": draft.get("editor_notes", ""),
            "warnings": warnings, "image_url": media_url, "story_type": story.triage.get("story_type"),
            "triage_score": story.triage.get("score"), "cluster_urls": story.cluster.urls[:8],
        }

        (self.out / f"{slug}.html").write_text(preview_html(draft, content, image_file), encoding="utf-8")
        if self.dry_run:
            result["edit_link"] = str((self.out / f"{slug}.html").resolve())
            result["preview_link"] = result["edit_link"]
            return result

        if (cfg["run"].get("skip_on_failed_fact_check") and fact.get("verdict") == "major_issues"):
            raise RuntimeError(f"Fact-check failed for '{draft['headline']}' — not drafted")

        cat_ids = []
        cats = self.wp.categories()
        for slug_name in draft["categories"]:
            if slug_name in cats:
                cat_ids.append(cats[slug_name])
        tag_ids, created = self.wp.resolve_tags(draft["tags"], cfg["wordpress"]["create_new_tags"])
        if created:
            result["warnings"].append("New tags created: " + ", ".join(created))
        if self.registered_meta is None or "_newsdesk_report" in self.registered_meta:
            # Shown to the editor in the "Newsdesk review notes" box on the edit screen.
            meta["_newsdesk_report"] = json.dumps({
                "warnings": result["warnings"], "seo": report.as_dict(), "fact_check": fact,
                "editor_notes": draft.get("editor_notes", ""), "image_prompt": draft["image"]["prompt"],
                "image_alt": draft["image"]["alt_text"],
                "revised": draft.get("_revised", False), "run_id": self.run_id})
        payload = {
            "title": draft["headline"], "content": content, "excerpt": draft["meta_description"],
            "slug": slug, "status": site.get("post_status", "draft"), "categories": cat_ids,
            "tags": tag_ids, "meta": meta, "format": "standard",
        }
        if site.get("author_id"):
            payload["author"] = int(site["author_id"])
        if media_id:
            payload["featured_media"] = media_id
        post = self.wp.create_post(payload)
        if media_id:
            self.wp.attach_media(media_id, post["id"])
        result.update(post_id=post["id"], edit_link=self.wp.edit_link(post["id"]),
                      preview_link=self.wp.preview_link(post["id"]))
        return result

    # ------------------------------------------------------------- run
    def run(self, max_drafts: int | None = None, urls: list[str] | None = None) -> dict:
        cfg = self.cfg
        max_n = max_drafts or int(cfg["run"]["max_drafts_per_run"])
        summary: dict = {"run_id": self.run_id, "dry_run": self.dry_run, "drafts": [], "errors": [],
                         "considered": 0}
        try:
            self.preflight()
            if urls:
                items = [Item(title=u, url=u, published=self.now) for u in urls]
                for it in items:  # use the page title for triage
                    art = fetch_article(it.url, self.http, None)
                    it.title = art.title or it.url
                    it.summary = art.text[:400]
                    it.publisher = art.site_name
                clusters = [Cluster(items=[it]) for it in items]
            else:
                found = [i for i in discover(cfg, self.http, self.now) if not self.state.is_seen(i.url)]
                clusters = [c for c in cluster_items(found)
                            if not self.state.covered_match(c.title, c.urls)]
                clusters.sort(key=lambda c: c.newest or self.now, reverse=True)
                clusters = clusters[: int(cfg["run"]["max_candidates_for_triage"])]
            summary["considered"] = len(clusters)
            if not clusters:
                log.info("No new candidate stories")
                return self.finish(summary)

            triage_rows = self.triage(clusters)
            summary["triage"] = [dict(r, title=clusters[r["index"]].title)
                                 for r in triage_rows if 0 <= r.get("index", -1) < len(clusters)]
            stories = select_stories(clusters, triage_rows, cfg, max_n, force_all=bool(urls))
            log.info("Selected %d stories", len(stories))
            # Everything triaged is "seen", so tomorrow's run doesn't re-score it.
            if not urls:
                self.state.mark_seen([u for c in clusters for u in c.urls], self.now)

            min_chars = int(cfg["run"]["min_source_chars"])
            for story in stories:
                title = story.cluster.title
                try:
                    gather_sources(story, cfg, self.http, self.robots)
                    if sum(len(s["text"]) for s in story.sources) < min_chars:
                        summary["errors"].append(f"{title}: not enough readable source text "
                                                 "(paywalled or blocked)")
                        continue
                    draft = self.write(story)
                    result = self.publish(story, draft)
                    summary["drafts"].append(result)
                    if not self.dry_run:
                        self.state.add_covered(title, story.cluster.urls, result.get("post_id"),
                                               result["headline"], self.now)
                except Exception as exc:
                    log.error("Story failed: %s\n%s", title, traceback.format_exc())
                    summary["errors"].append(f"{title}: {exc}")
                    summary["failures"] = summary.get("failures", 0) + 1
                    # let the next run retry stories that failed for transient reasons
                    for u in story.cluster.urls:
                        self.state.seen.pop(url_key(u), None)
        except Exception as exc:
            log.error("Run failed: %s\n%s", exc, traceback.format_exc())
            summary["fatal"] = str(exc)
        return self.finish(summary)

    def finish(self, summary: dict) -> dict:
        u = self.claude.usage
        summary["usage"] = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                            "web_searches": u.web_searches, "calls": u.calls, "by_model": u.by_model}
        if not self.dry_run:
            self.state.save(self.now)
        summary["alerts_sent"] = notify.send_all(self.cfg, self.http, summary) if not self.dry_run else []
        (self.out / "run.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        (self.out / "alert.txt").write_text(notify.summary_text(summary), encoding="utf-8")
        (self.out / "alert.html").write_text(notify.summary_html(summary), encoding="utf-8")
        return summary
