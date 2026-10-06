"""Test doubles: a fake HTTP client that serves fixture feeds/articles and
emulates the Claude, OpenAI, WordPress and Slack endpoints."""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, status=200, body="", headers=None, json_data=None, url=""):
        self.status_code = status
        if json_data is not None:
            body = json.dumps(json_data)
        self._body = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.headers.setdefault("content-type", "application/json" if json_data is not None else "text/html")
        self.url = url

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    @property
    def content(self):
        return bytes(self._body)

    def json(self):
        return json.loads(self.text)


class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def tiny_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1536, 1024), (20, 70, 120)).save(buf, "PNG")
    return buf.getvalue()


class FakeClaude:
    """Returns canned responses keyed on which role is calling."""

    def __init__(self):
        self.calls: list[dict] = []
        self.writer_responses: list[dict] = []
        self.fact_responses: list[dict] = []
        self.triage_response: dict | None = None
        self.reject_structured = False

    def handle(self, payload: dict) -> FakeResponse:
        self.calls.append(payload)
        system = payload.get("system", "")
        if self.reject_structured and "output_config" in payload:
            return FakeResponse(400, json_data={"type": "error", "error": {
                "type": "invalid_request_error", "message": "output_config: not supported"}})
        if payload.get("tools"):
            text = ("- Primary source: Journal of Fixture Biology paper led by Dr. Mara Quill "
                    "(https://doi.org/10.0000/fixture.2026.001)\n")
            search_block = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": "https://www.nbclosangeles.example/news/shark-hearing/",
                 "title": "Sharks hear from far away", "encrypted_content": "x", "page_age": "Oct 1, 2026"}]}
            return self._msg(payload, [search_block, {"type": "text", "text": text, "citations": [
                {"type": "web_search_result_location", "url": "https://doi.org/10.0000/fixture.2026.001",
                 "title": "Blacktip hearing range", "cited_text": "blacktip sharks responded to sounds"}]}],
                server_tool_use={"web_search_requests": 1})
        if system.startswith("You are the assignment editor"):
            data = self.triage_response or default_triage(payload)
        elif system.startswith("You are a staff writer"):
            data = self.writer_responses.pop(0) if self.writer_responses else good_draft()
        elif system.startswith("You are Sharkophile's copy chief"):
            data = self.fact_responses.pop(0) if self.fact_responses else {
                "verdict": "pass", "issues": [], "summary": "All claims supported."}
        else:
            data = {"ok": True}
        return self._msg(payload, [{"type": "text", "text": json.dumps(data)}])

    @staticmethod
    def _msg(payload, content, server_tool_use=None):
        usage = {"input_tokens": 1000, "output_tokens": 500}
        if server_tool_use:
            usage["server_tool_use"] = server_tool_use
        return FakeResponse(200, json_data={"id": "msg_x", "type": "message", "role": "assistant",
                                            "model": payload["model"], "content": content,
                                            "stop_reason": "end_turn", "usage": usage})

    def writer_calls(self):
        return [c for c in self.calls if c.get("system", "").startswith("You are a staff writer")]


def default_triage(payload: dict) -> dict:
    text = payload["messages"][0]["content"]
    rows = json.loads(text.split("Candidates (JSON):\n", 1)[1].rsplit("\n\nReturn one entry", 1)[0])
    stories = []
    for r in rows:
        t = r["title"].lower()
        hearing = "hear" in t
        flood = "flooded" in t
        stories.append({
            "index": r["index"], "is_shark_story": True, "already_covered": False, "same_story_as": -1,
            "score": 8 if hearing else (7 if flood else 4),
            "story_type": "science" if hearing else ("odd" if flood else "news"),
            "focus_species": "blacktip shark" if hearing else "",
            "search_terms": ["blacktip", "shark hearing"] if hearing else ["shark sighting"],
            "reason": "test",
        })
    return {"stories": stories}


QUOTE = ("We were surprised how far away these sharks could pick out a sound and then swim "
         "straight toward it")


def good_draft() -> dict:
    return {
        "headline": "Blacktip sharks can hear prey sounds nearly 250 feet away, study finds",
        "seo_title": "Shark hearing study: blacktips detect sound 250 feet away",
        "slug": "shark-hearing-study-blacktips-250-feet",
        "focus_keyword": "shark hearing study",
        "meta_description": ("A new shark hearing study found blacktip sharks can detect and home in on "
                             "low-frequency sounds from nearly 250 feet away in open water."),
        "body": [
            {"type": "paragraph", "text": (
                "Blacktip sharks can detect low-frequency sounds from as far as 243 feet away and swim "
                "directly toward the source, according to a shark hearing study published by researchers "
                "at the <a href=\"https://www.fixture-university.edu/news/shark-hearing\">Fixture University</a> "
                "marine lab."), "items": [], "attribution": ""},
            {"type": "paragraph", "text": (
                "The team played recorded fish sounds through underwater speakers off the Florida coast and "
                "tracked 31 tagged blacktips as they responded."), "items": [], "attribution": ""},
            {"type": "paragraph", "text": f"“{QUOTE},” said Dr. Mara Quill, the study’s lead author.",
             "items": [], "attribution": ""},
            {"type": "paragraph", "text": (
                "The findings, published in the <a href=\"https://doi.org/10.0000/fixture.2026.001\">Journal "
                "of Fixture Biology</a>, suggest hearing may matter more to how sharks find food than "
                "scientists assumed. Sharkophile has previously covered "
                "<a href=\"https://www.sharkophile.com/blacktip-migration-florida/\">blacktip migration "
                "along Florida's Atlantic coast</a>."), "items": [], "attribution": ""},
            {"type": "paragraph", "text": (
                "Researchers said the sounds most likely to attract the sharks were the low thumps made by "
                "struggling fish, which travel farther underwater than higher-pitched noises. The team "
                "now plans to test whether boat noise masks those signals in busy harbors and inlets."),
             "items": [], "attribution": ""},
            {"type": "paragraph", "text": (
                "Blacktips are common in shallow coastal water from North Carolina to Texas, where they "
                "gather in large numbers each winter. The study did not test whether other species hear "
                "as well, and the authors said more work is needed before the results can be applied to "
                "larger sharks such as bull sharks or tiger sharks. Earlier lab experiments had suggested "
                "a much shorter range, but those tests were run in tanks where echoes distort sound. "
                "Field tests like this one remain rare because tracking free-swimming sharks is costly "
                "and slow, the researchers noted, and the team hopes other groups will repeat the work "
                "in different habitats and seasons over the next few years."),
             "items": [], "attribution": ""},
        ],
        "categories": ["news", "science", "biology", "Featured"],
        "tags": ["blacktip", "#SharkResearch", "shark hearing", "Florida"],
        "image": {"prompt": "Three blacktip sharks cruising over a sandy seafloor in clear green water, "
                            "sunbeams from above",
                  "alt_text": "Three blacktip sharks swim over a sandy seafloor in sunlit green water"},
        "social_message": "Blacktip sharks can hear struggling fish from nearly 250 feet away. #sharks",
        "sources": [
            {"title": "Sharks can hear nearly 250 feet away", "publisher": "Fixture University",
             "url": "https://www.fixture-university.edu/news/shark-hearing", "role": "press_release"},
            {"title": "Blacktip hearing range", "publisher": "Journal of Fixture Biology",
             "url": "https://doi.org/10.0000/fixture.2026.001", "role": "study"},
            {"title": "Made-up source", "publisher": "Nowhere", "url": "https://invented.example.com/x",
             "role": "secondary"},
        ],
        "claims": [{"claim": "243 feet", "source_ids": ["S1"]}],
        "editor_notes": "Confirm the number of tagged sharks against the paper.",
        "confidence": "high",
    }


def bad_quote_draft() -> dict:
    d = good_draft()
    d["body"][2]["text"] = ("“Sharks are basically swimming ears and this changes everything we know,” "
                            "said Dr. Mara Quill.")
    return d


class FakeWordPress:
    def __init__(self, meta_keys=None, strip_authorization=False, plugin_installed=True):
        # strip_authorization emulates Bluehost-style Apache dropping the Authorization
        # header; plugin_installed controls whether the X-Newsdesk-Authorization fallback works.
        self.strip_authorization = strip_authorization
        self.plugin_installed = plugin_installed
        self.posts: list[dict] = []
        self.media: list[dict] = []
        self.tags = [{"id": 59, "name": "blacktip", "slug": "blacktip", "count": 14},
                     {"id": 5, "name": "Great white", "slug": "great-white", "count": 78},
                     {"id": 399, "name": "#SharkResearch", "slug": "sharkresearch", "count": 4}]
        self.cats = [{"id": 13, "slug": "news", "name": "News"}, {"id": 4, "slug": "science", "name": "Science"},
                     {"id": 22, "slug": "biology", "name": "Biology"}, {"id": 29, "slug": "odd", "name": "Odd"},
                     {"id": 2, "slug": "featured", "name": "Featured"}]
        self.meta_keys = meta_keys if meta_keys is not None else [
            "_genesis_title", "_genesis_description", "_newsdesk_sources", "_newsdesk_report",
            "_newsdesk_focus_keyword", "_newsdesk_run_id", "jetpack_publicize_message"]
        self.next_id = 9000

    def handle(self, method, path, params, headers, body):
        authed = ("Authorization" in headers and not self.strip_authorization) or \
                 ("X-Newsdesk-Authorization" in headers and self.plugin_installed)
        if path == "/users/me":
            if not authed:
                return FakeResponse(401, json_data={"code": "rest_not_logged_in", "message": "nope"})
            return FakeResponse(json_data={"id": 7, "name": "Newsdesk Bot", "slug": "newsdesk",
                                           "roles": ["editor"], "capabilities": {
                                               "edit_posts": True, "upload_files": True,
                                               "manage_categories": True}})
        if method == "OPTIONS" and path == "/posts":
            return FakeResponse(json_data={"schema": {"properties": {"meta": {"properties": {
                k: {"type": "string"} for k in self.meta_keys}}}}})
        if path == "/categories":
            return FakeResponse(json_data=self.cats, headers={"X-WP-TotalPages": "1"})
        if path == "/tags" and method == "GET":
            return FakeResponse(json_data=self.tags, headers={"X-WP-TotalPages": "1"})
        if path == "/tags" and method == "POST":
            name = json.loads(body)["name"]
            tag = {"id": self.next_id, "name": name, "slug": name.lower().replace(" ", "-"), "count": 0}
            self.next_id += 1
            self.tags.append(tag)
            return FakeResponse(201, json_data=tag)
        if path == "/posts" and method == "GET":
            if params.get("search"):
                return FakeResponse(json_data=[{"id": 4100, "title": {"rendered": "Blacktip migration along Florida’s Atlantic coast"},
                                                "link": "https://www.sharkophile.com/blacktip-migration-florida/",
                                                "date": "2024-01-10T10:00:00"}])
            return FakeResponse(json_data=[{"id": p["id"], "title": {"raw": p["title"], "rendered": p["title"]},
                                            "status": p["status"]} for p in self.posts])
        if path == "/posts" and method == "POST":
            post = json.loads(body)
            post["id"] = self.next_id
            self.next_id += 1
            self.posts.append(post)
            return FakeResponse(201, json_data={"id": post["id"], "status": post["status"],
                                                "link": f"https://www.sharkophile.com/?p={post['id']}"})
        if path == "/media" and method == "POST":
            m = {"id": self.next_id, "bytes": len(body), "disposition": headers.get("Content-Disposition"),
                 "mime": headers.get("Content-Type")}
            self.next_id += 1
            self.media.append(m)
            return FakeResponse(201, json_data={"id": m["id"], "source_url": f"https://www.sharkophile.com/wp-content/uploads/{m['id']}.jpg",
                                                "media_details": {"sizes": {"large": {"source_url": f"https://www.sharkophile.com/wp-content/uploads/{m['id']}-1024x683.jpg"}}}})
        m = re.match(r"/media/(\d+)$", path)
        if m and method == "POST":
            media = next(x for x in self.media if x["id"] == int(m.group(1)))
            media.update(json.loads(body))
            return FakeResponse(json_data={"id": media["id"]})
        return FakeResponse(404, json_data={"code": "rest_no_route", "message": path})


class FakeHttp:
    def __init__(self, claude: FakeClaude | None = None, wp: FakeWordPress | None = None,
                 articles: dict | None = None, feeds: dict | None = None):
        self.claude = claude or FakeClaude()
        self.wp = wp or FakeWordPress()
        self.articles = articles if articles is not None else {
            "https://www.fixture-university.edu/news/shark-hearing": (FIXTURES / "article_hearing.html").read_text(),
            "https://www.upi.com/odd_news/2026/09/30/shark-flooded-road-surf-city-new-jersey/2471790789410": (FIXTURES / "article_flood.html").read_text(),
        }
        self.feeds = feeds if feeds is not None else {
            "bing": (FIXTURES / "bing_shark.xml").read_text(),
            "sciencedaily": (FIXTURES / "sciencedaily_fish.xml").read_text(),
        }
        self.log: list[tuple[str, str]] = []
        self.slack: list[dict] = []
        self.openai_calls: list[dict] = []
        self._png = None

    def request(self, method, url, params=None, headers=None, data=None, **kw):
        headers = _Headers(headers or {})
        self.log.append((method, url))
        p = urlparse(url)
        if p.netloc == "api.anthropic.com":
            return self.claude.handle(json.loads(data))
        if p.netloc == "api.openai.com" and p.path == "/v1/images/generations":
            self.openai_calls.append(json.loads(data))
            if getattr(self, "openai_no_credits", False):
                return FakeResponse(429, json_data={"error": {
                    "message": "You have no credits remaining. Add credits to continue using the API.",
                    "type": "insufficient_quota", "code": "credit_balance_exhausted"}})
            if self._png is None:
                self._png = tiny_png()
            return FakeResponse(json_data={"data": [{"b64_json": base64.b64encode(self._png).decode()}]})
        if p.netloc == "hooks.slack.com":
            self.slack.append(json.loads(data))
            return FakeResponse(200, "ok", headers={"content-type": "text/plain"})
        if p.netloc == "www.sharkophile.com" and p.path.startswith("/wp-json/wp/v2"):
            params = dict(params or {})
            params.update({k: v[0] for k, v in parse_qs(p.query).items()})
            return self.wp.handle(method, p.path[len("/wp-json/wp/v2"):], params, headers, data)
        if p.path == "/robots.txt":
            return FakeResponse(200, "User-agent: *\nDisallow: /private/\n", headers={"content-type": "text/plain"})
        if p.netloc == "www.bing.com":
            q = parse_qs(p.query).get("q", [""])[0]
            body = self.feeds["bing"] if q == "shark" else "<rss><channel></channel></rss>"
            return FakeResponse(200, body, headers={"content-type": "application/rss+xml"})
        if "sciencedaily.com/rss" in url:
            return FakeResponse(200, self.feeds["sciencedaily"], headers={"content-type": "application/rss+xml"})
        if url.rstrip("/") in self.articles:
            return FakeResponse(200, self.articles[url.rstrip("/")], headers={"content-type": "text/html; charset=utf-8"}, url=url)
        if "/rss" in url or "feed" in url:
            return FakeResponse(200, "<rss><channel></channel></rss>", headers={"content-type": "application/rss+xml"})
        return FakeResponse(404, "not found")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def posts_to(self, host: str) -> list[str]:
        return [u for m, u in self.log if m == "POST" and host in u]
