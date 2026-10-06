"""Prompts and JSON schemas for the three model roles: triage editor, writer,
and fact-checker. The house style guide (STYLE_GUIDE.md) is embedded in the
writer and fact-checker system prompts."""

from __future__ import annotations

import json

STORY_TYPES = ["news", "science", "conservation", "attack", "entertainment", "events",
               "fishing", "diving", "odd"]

# --------------------------------------------------------------------- triage

TRIAGE_SYSTEM = """You are the assignment editor for Sharkophile.com, a shark news site
("the sharkiest place on the web"). You decide which candidate stories are worth a
write-up today. Readers want shark science, conservation and policy, notable
sightings and incidents, fishing and diving news, and shark pop culture (Shark
Week, SharkFest, films, books, viral moments).

Score each candidate 0–10:
- 9–10: major news — new species, landmark study in a major journal, fatal or
  widely reported bite confirmed by officials, significant law/fisheries decision,
  Shark Week/SharkFest announcements, a story every shark fan will hear about.
- 7–8: solid — new peer-reviewed study, conservation milestone, verified unusual
  behavior or sighting with expert comment, notable entertainment release.
- 5–6: minor or local — routine sighting, small event, soft feature.
- 0–4: skip — not actually about sharks/rays (sports teams named Sharks, Shark Tank,
  SharkNinja, loan/card/pool sharks, the Baby Shark song unless genuine pop-culture
  news), listicles, evergreen explainers, product promos, stale items re-surfaced,
  speculation without sources, or stories already covered.

Mark same_story_as when a candidate covers the same event/study as an EARLIER
candidate in the list (use that earlier index). Mark already_covered when it matches
a recent Sharkophile headline. For incidents, only score above 6 if officials or
reputable outlets confirm it."""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "is_shark_story": {"type": "boolean"},
                    "already_covered": {"type": "boolean"},
                    "same_story_as": {"type": "integer", "description": "earlier index, or -1"},
                    "score": {"type": "integer", "description": "0-10"},
                    "story_type": {"type": "string", "enum": STORY_TYPES},
                    "focus_species": {"type": "string", "description": "e.g. 'great white shark', or ''"},
                    "search_terms": {"type": "array", "items": {"type": "string"},
                                     "description": "2-3 short terms to find related Sharkophile posts"},
                    "reason": {"type": "string", "description": "one short sentence"},
                },
                "required": ["index", "is_shark_story", "already_covered", "same_story_as", "score",
                             "story_type", "focus_species", "search_terms", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stories"],
    "additionalProperties": False,
}


def triage_user(candidates: list[dict], recent_headlines: list[str], today: str) -> str:
    return (
        f"Today is {today}.\n\nRecent Sharkophile headlines (already covered):\n"
        + ("\n".join(f"- {h}" for h in recent_headlines) or "- (none)")
        + "\n\nCandidates (JSON):\n" + json.dumps(candidates, ensure_ascii=False, indent=1)
        + "\n\nReturn one entry per candidate index."
    )


# --------------------------------------------------------------------- writer

BLOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["paragraph", "heading", "list", "quote"]},
        "text": {"type": "string", "description": "Block text. Inline <a href>, <em>, <strong> only. "
                 "For lists: an optional lead-in sentence, else ''."},
        "items": {"type": "array", "items": {"type": "string"}, "description": "List items, else []"},
        "attribution": {"type": "string", "description": "For quote blocks: 'Name, title', else ''"},
    },
    "required": ["type", "text", "items", "attribution"],
    "additionalProperties": False,
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "Sentence case, <= 70 chars"},
        "seo_title": {"type": "string", "description": "<= 60 chars, focus keyword near the front"},
        "slug": {"type": "string", "description": "3-7 lowercase hyphenated words"},
        "focus_keyword": {"type": "string"},
        "meta_description": {"type": "string", "description": "120-155 chars, one sentence, includes focus keyword"},
        "body": {"type": "array", "items": BLOCK_SCHEMA},
        "categories": {"type": "array", "items": {"type": "string"}, "description": "category slugs"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "image": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Scene description for an editorial illustration"},
                "alt_text": {"type": "string", "description": "<= 125 chars, describes what is shown"},
            },
            "required": ["prompt", "alt_text"],
            "additionalProperties": False,
        },
        "social_message": {"type": "string", "description": "<= 200 chars for social sharing; up to 2 hashtags"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "publisher": {"type": "string"},
                    "url": {"type": "string"},
                    "role": {"type": "string", "enum": ["primary", "secondary", "study", "press_release"]},
                },
                "required": ["title", "publisher", "url", "role"],
                "additionalProperties": False,
            },
        },
        "claims": {
            "type": "array",
            "description": "Every factual claim in the story with the source id(s) that support it",
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string"}, "source_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["claim", "source_ids"],
                "additionalProperties": False,
            },
        },
        "editor_notes": {"type": "string", "description": "What the editor should double-check; unverified items; conflicts"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["headline", "seo_title", "slug", "focus_keyword", "meta_description", "body",
                 "categories", "tags", "image", "social_message", "sources", "claims",
                 "editor_notes", "confidence"],
    "additionalProperties": False,
}


def writer_system(style_guide: str) -> str:
    return (
        "You are a staff writer for Sharkophile.com. Write news drafts that a human editor "
        "will review before publishing. Follow the house style guide below exactly — it "
        "overrides any habit of yours.\n\n"
        "Hard rules:\n"
        "1. Use ONLY facts found in the numbered source material. If something isn't there, "
        "leave it out and mention the gap in editor_notes.\n"
        "2. Write entirely in your own words. Never reuse a source's sentences or distinctive "
        "phrasing outside quotation marks.\n"
        "3. Quotes must be copied character-for-character from a source, attributed to the "
        "speaker (and 'told <Outlet>' if from another outlet's interview). Max three. Put quotes "
        "inside paragraphs in house style (“…,” Name said.). Use a `quote` block at most once, "
        "for a standout line, and put only the quoted words in it (no quotation marks).\n"
        "4. Link the primary source (study DOI, agency release, official statement) inline with "
        "descriptive anchor text, and link the outlet you relied on most. Use only URLs that "
        "appear in the source material or the web research results.\n"
        "5. If a related Sharkophile story is provided and genuinely related, link it once, "
        "naturally, in the context section.\n"
        "6. Don't add the 'Source:' line or an image — the system adds those.\n"
        "7. The headline is rendered as the H1; body headings are H2.\n\n"
        "=== HOUSE STYLE GUIDE ===\n" + style_guide
    )


def writer_user(*, today: str, triage: dict, sources: list[dict], research_notes: str,
                research_citations: list[dict], related_posts: list[dict], existing_tags: list[str],
                allowed_categories: list[str]) -> str:
    src_parts = []
    for s in sources:
        src_parts.append(
            f"[{s['id']}] {s.get('title','')}\nOutlet: {s.get('publisher','')}\nURL: {s['url']}\n"
            f"Published: {s.get('published','')}\n"
            + (f"Links found in article (possible primary sources): {', '.join(s.get('primary_links', [])[:8])}\n"
               if s.get("primary_links") else "")
            + f"---\n{s['text']}\n"
        )
    research = ""
    if research_notes:
        cites = "\n".join(f"- {c['title']} — {c['url']}" + (f" (\"{c['cited_text']}\")" if c.get('cited_text') else "")
                          for c in research_citations)
        research = (f"\n=== [R] WEB RESEARCH NOTES (treat as a source; only use facts backed by the "
                    f"cited URLs) ===\n{research_notes}\nCited:\n{cites}\n")
    related = "\n".join(f"- {p['title']} — {p['link']}" for p in related_posts) or "- (none found)"
    return (
        f"Today is {today} (Eastern Time).\n\n"
        f"Assignment: {triage.get('story_type','news')} story. Focus species: "
        f"{triage.get('focus_species') or 'n/a'}. Editor's note: {triage.get('reason','')}\n\n"
        "=== SOURCE MATERIAL ===\n" + "\n".join(src_parts) + research +
        f"\n=== RELATED SHARKOPHILE STORIES (for one internal link) ===\n{related}\n"
        f"\n=== EXISTING TAGS (prefer these) ===\n{', '.join(existing_tags[:150])}\n"
        f"\n=== CATEGORY SLUGS YOU MAY USE ===\n{', '.join(allowed_categories)}\n"
        "Always include 'news' plus 1–2 topical categories.\n\n"
        "For `image.prompt`, describe a calm, scientifically accurate scene for an editorial "
        "illustration (species, setting, light, composition). No people, no text, no blood, "
        "no re-creation of a real incident.\n"
        "In `sources`, list each source you used with its real URL; role 'study' for the paper, "
        "'press_release' for institution releases, 'primary' for the main news outlet.\n"
        "Write the draft now."
    )


def revise_user(previous: dict, problems: list[str]) -> str:
    return (
        "Revise this draft to fix every problem listed. Keep everything else that is correct. "
        "Return the full draft in the same JSON shape.\n\nProblems:\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\n\nPrevious draft (JSON):\n" + json.dumps(previous, ensure_ascii=False)
    )


# ----------------------------------------------------------------- fact check

FACT_CHECK_SYSTEM = """You are Sharkophile's copy chief. Compare a draft news story against its
source material and find factual problems before a human editor sees it.

Flag as MAJOR: any number, name, date, place, species, quote, finding or attribution
that is not supported by the sources or contradicts them; quotes that are altered or
attributed to the wrong person; claims stated more strongly than the source supports
("proves" vs "suggests"); invented study details; links that don't appear in the sources.
Flag as MINOR: missing context the source provides, awkward hedging, style-guide
violations, an unclear attribution.
The italic "Source:" line, the featured image and its caption are added automatically
after this check, so never flag them as missing.
Do not flag well-established general background (e.g. "sharks are fish", "great
whites are found worldwide") unless it is wrong. Be specific and brief."""

FACT_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "minor_issues", "major_issues"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["minor", "major"]},
                    "excerpt": {"type": "string", "description": "the draft text in question"},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "excerpt", "problem", "fix"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "issues", "summary"],
    "additionalProperties": False,
}


def fact_check_user(draft_text: str, headline: str, meta: str, source_bundle: str, link_list: list[str]) -> str:
    return (
        f"HEADLINE: {headline}\nMETA DESCRIPTION: {meta}\nLINKS USED: {', '.join(link_list) or '(none)'}\n\n"
        f"=== DRAFT ===\n{draft_text}\n\n=== SOURCE MATERIAL ===\n{source_bundle}"
    )


# ------------------------------------------------------------------- research

RESEARCH_SYSTEM = """You are a research assistant for a shark news desk. Using web search,
find the PRIMARY source behind a news story: the peer-reviewed paper (journal, title,
lead author, institution, DOI link), the institution's press release, or the official
agency statement. Report only facts you can cite from search results. Be concise:
bullet points, each with the URL it came from. If you cannot find the primary source,
say so plainly."""


def research_user(title: str, urls: list[str], summary: str) -> str:
    return (f"Story: {title}\nCoverage so far: {', '.join(urls[:4])}\nSummary: {summary}\n\n"
            "Find the primary source and any key facts the coverage may have gotten wrong.")


# --------------------------------------------------------------- illustrate

IMAGE_BRIEF_SYSTEM = """You are Sharkophile's photo editor. Describe ONE featured illustration for
a shark news story, for an AI image model. Follow the house image rules exactly:
- Editorial illustration, anatomically accurate for the species named.
- Never: identifiable people, a re-creation of the actual incident, blood or injury,
  logos, text, movie characters, or anything that could pass for news photography of
  the event.
- For bites and incidents: show the species calmly in open water, or an empty
  coastline, not the event itself.
- For entertainment: an evocative, generic scene, no copyrighted characters.
Describe species, setting, light and composition in 1–3 sentences. The alt text says
plainly what the picture shows, 125 characters or fewer, without "image of"."""

IMAGE_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "alt_text": {"type": "string"},
    },
    "required": ["prompt", "alt_text"],
    "additionalProperties": False,
}


def image_brief_user(title: str, excerpt: str, body_text: str) -> str:
    return f"Headline: {title}\nSummary: {excerpt}\n\nStory:\n{body_text[:3000]}"
