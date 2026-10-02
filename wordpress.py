"""WordPress REST API client (application-password auth)."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .http import HttpError

log = logging.getLogger(__name__)

SEO_META_KEYS = {
    "genesis": {"title": "_genesis_title", "description": "_genesis_description"},
    "yoast": {"title": "_yoast_wpseo_title", "description": "_yoast_wpseo_metadesc",
              "keyword": "_yoast_wpseo_focuskw"},
    "rankmath": {"title": "rank_math_title", "description": "rank_math_description",
                 "keyword": "rank_math_focus_keyword"},
    "jetpack": {"title": "jetpack_seo_html_title", "description": "advanced_seo_description"},
    "none": {},
}


class WordPressError(RuntimeError):
    pass


class WordPress:
    def __init__(self, base_url: str, user: str | None, app_password: str | None, http):
        self.base = base_url.rstrip("/")
        self.api = self.base + "/wp-json/wp/v2"
        self.http = http
        self.auth_header = None
        if user and app_password:
            token = base64.b64encode(f"{user}:{app_password.replace(' ', '')}".encode()).decode()
            # Same credentials twice: many Apache hosts drop "Authorization" before PHP
            # sees it; the companion plugin reads the X-Newsdesk-Authorization copy.
            self.auth_header = {"Authorization": f"Basic {token}",
                                "X-Newsdesk-Authorization": f"Basic {token}"}
        self._categories: dict[str, int] | None = None
        self._tags: dict[str, dict] | None = None

    # ------------------------------------------------------------- plumbing
    def _headers(self, extra: dict | None = None, auth: bool = True) -> dict:
        h = {"Accept": "application/json"}
        if auth and self.auth_header:
            h.update(self.auth_header)
        if extra:
            h.update(extra)
        return h

    def _check(self, resp, what: str):
        if resp.status_code >= 400:
            detail = resp.text[:400]
            try:
                body = resp.json()
                detail = f"{body.get('code')}: {body.get('message')}"
            except Exception:
                pass
            raise WordPressError(f"{what} failed ({resp.status_code}): {detail}")
        return resp.json()

    def _get(self, path: str, params: dict | None = None, auth: bool = True):
        resp = self.http.get(self.api + path, params=params or {}, headers=self._headers(auth=auth))
        return resp, self._check(resp, f"GET {path}")

    def _get_all(self, path: str, params: dict, auth: bool = True, max_pages: int = 10) -> list:
        out, page = [], 1
        while page <= max_pages:
            resp, data = self._get(path, dict(params, per_page=100, page=page), auth=auth)
            out.extend(data)
            total = int(resp.headers.get("X-WP-TotalPages", "1") or 1)
            if page >= total:
                break
            page += 1
        return out

    # ---------------------------------------------------------------- checks
    def whoami(self) -> dict:
        if not self.auth_header:
            raise WordPressError("WP_USER / WP_APP_PASSWORD are not set")
        try:
            _, data = self._get("/users/me", {"context": "edit"})
        except WordPressError as exc:
            if "rest_not_logged_in" in str(exc):
                raise WordPressError(
                    "WordPress ignored the login (rest_not_logged_in). The host is stripping the "
                    "Authorization header: install/activate the Sharkophile Newsdesk Support plugin "
                    "(wordpress/sharkophile-newsdesk.php), which accepts the newsdesk's backup header."
                ) from exc
            raise
        return data

    def registered_post_meta(self) -> set[str]:
        """Meta keys the REST API will accept on posts (from the schema)."""
        resp = self.http.request("OPTIONS", self.api + "/posts", headers=self._headers())
        data = self._check(resp, "OPTIONS /posts")
        try:
            props = data["schema"]["properties"]["meta"]["properties"]
            return set(props.keys())
        except (KeyError, TypeError):
            return set()

    # ------------------------------------------------------------- taxonomy
    def categories(self) -> dict[str, int]:
        if self._categories is None:
            cats = self._get_all("/categories", {"_fields": "id,slug,name"}, auth=False)
            self._categories = {c["slug"]: c["id"] for c in cats}
        return self._categories

    def tags(self) -> dict[str, dict]:
        """name.lower() -> {id, name, count}"""
        if self._tags is None:
            tags = self._get_all("/tags", {"_fields": "id,name,slug,count", "orderby": "count",
                                           "order": "desc"}, auth=False, max_pages=5)
            self._tags = {t["name"].lower(): t for t in tags}
            for t in tags:
                self._tags.setdefault(t["slug"].replace("-", " "), t)
        return self._tags

    def popular_tag_names(self, limit: int = 150) -> list[str]:
        seen, names = set(), []
        for t in sorted(self.tags().values(), key=lambda t: -t.get("count", 0)):
            if t["id"] in seen or t["name"].startswith("#"):
                continue
            seen.add(t["id"])
            names.append(t["name"])
        return names[:limit]

    def resolve_tags(self, names: list[str], create: bool) -> tuple[list[int], list[str]]:
        ids, created = [], []
        existing = self.tags()
        for name in names:
            clean = re.sub(r"^#", "", name).strip()
            if not clean:
                continue
            match = existing.get(clean.lower()) or existing.get(clean.lower().rstrip("s"))
            if match and match["name"].startswith("#"):
                # Legacy hashtag tags (2023 era) aren't reused; house style is plain tags.
                log.info("Skipping hashtag-style tag match for '%s'", clean)
                continue
            if match:
                ids.append(match["id"])
            elif create:
                resp = self.http.post(self.api + "/tags", headers=self._headers(
                    {"Content-Type": "application/json"}), data=json.dumps({"name": clean}))
                if resp.status_code == 400 and "term_exists" in resp.text:
                    term_id = resp.json().get("data", {}).get("term_id")
                    if term_id:
                        ids.append(int(term_id))
                    continue
                tag = self._check(resp, f"create tag {clean}")
                existing[clean.lower()] = tag
                ids.append(tag["id"])
                created.append(clean)
        return list(dict.fromkeys(ids)), created

    # ---------------------------------------------------------------- posts
    def search_posts(self, term: str, limit: int = 4) -> list[dict]:
        _, data = self._get("/posts", {"search": term, "per_page": limit,
                                       "_fields": "id,title,link,date"}, auth=False)
        return [{"id": p["id"], "title": re.sub(r"<[^>]+>", "", p["title"]["rendered"]),
                 "link": p["link"], "date": p["date"]} for p in data]

    def recent_titles(self, days: int = 30) -> list[str]:
        after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        params = {"after": after, "per_page": 100, "_fields": "id,title,status",
                  "status": "publish,future,draft,pending", "context": "edit"}
        try:
            _, data = self._get("/posts", params)
        except WordPressError:
            _, data = self._get("/posts", {"after": after, "per_page": 100, "_fields": "id,title"},
                                auth=False)
        titles = []
        for p in data:
            t = p.get("title", {})
            titles.append(t.get("raw") or re.sub(r"<[^>]+>", "", t.get("rendered", "")))
        return [t for t in titles if t]

    def upload_media(self, data: bytes, filename: str, mime: str, alt: str, caption: str,
                     title: str, description: str = "") -> dict:
        headers = self._headers({
            "Content-Type": mime,
            "Content-Disposition": f'attachment; filename="{quote(filename)}"',
        })
        resp = self.http.post(self.api + "/media", headers=headers, data=data, timeout=180)
        media = self._check(resp, "upload media")
        resp = self.http.post(self.api + f"/media/{media['id']}",
                              headers=self._headers({"Content-Type": "application/json"}),
                              data=json.dumps({"alt_text": alt, "caption": caption, "title": title,
                                               "description": description}))
        updated = self._check(resp, "update media")
        # keep URLs/sizes from the upload response if the update omits them
        merged = dict(media)
        merged.update({k: v for k, v in (updated or {}).items() if v not in (None, "", {}, [])})
        return merged

    def create_post(self, payload: dict) -> dict:
        resp = self.http.post(self.api + "/posts",
                              headers=self._headers({"Content-Type": "application/json"}),
                              data=json.dumps(payload), timeout=90)
        return self._check(resp, "create post")

    def attach_media(self, media_id: int, post_id: int) -> None:
        resp = self.http.post(self.api + f"/media/{media_id}",
                              headers=self._headers({"Content-Type": "application/json"}),
                              data=json.dumps({"post": post_id}))
        if resp.status_code >= 400:
            log.warning("Could not attach media %s to post %s: %s", media_id, post_id, resp.text[:200])

    def edit_link(self, post_id: int) -> str:
        return f"{self.base}/wp-admin/post.php?post={post_id}&action=edit"

    def preview_link(self, post_id: int) -> str:
        return f"{self.base}/?p={post_id}&preview=true"


def seo_meta(plugin: str, seo_title: str, description: str, keyword: str,
             registered: set[str] | None) -> tuple[dict, list[str]]:
    """Meta fields for the configured SEO plugin, limited to keys WordPress will accept."""
    keys = SEO_META_KEYS.get(plugin, {})
    values = {"title": seo_title, "description": description, "keyword": keyword}
    meta, skipped = {}, []
    for field, key in keys.items():
        if registered is not None and key not in registered:
            skipped.append(key)
            continue
        meta[key] = values[field]
    return meta, skipped
