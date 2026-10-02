"""Configuration: config.yaml for behavior, environment variables for secrets."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "site": {
        "url": "https://www.sharkophile.com",
        "name": "Sharkophile",
        "timezone": "America/New_York",
        "author_id": None,
        "post_status": "draft",
        "embed_image_in_content": True,
        "seo_plugin": "genesis",
    },
    "run": {
        "max_drafts_per_run": 3,
        "max_age_hours": 48,
        "min_score": 6,
        "diversity_override_score": 9,
        "auto_revise": True,
        "web_research": True,
        "web_research_max_searches": 3,
        "min_source_chars": 700,
        "skip_on_failed_fact_check": False,
        "max_candidates_for_triage": 60,
    },
    "models": {
        "triage": "claude-haiku-4-5-20251001",
        "writer": "claude-opus-5-5",
        "fact_check": "claude-sonnet-5-5",
    },
    "images": {
        "provider": "openai",
        "model": "gpt-image-2",
        "size": "1536x1024",
        "quality": "medium",
        "output_format": "jpeg",
        "output_width": 1200,
        "caption": "Illustration generated with AI for Sharkophile.",
    },
    "alerts": {"slack": True, "email": True, "webhook": False, "notify_on_empty_run": False},
    "sources": {
        "bing_queries": ["shark"],
        "feeds": [],
        "keywords": ["shark"],
        "exclude_patterns": [],
        "blocked_domains": [],
        "preferred_domains": [],
    },
    "wordpress": {
        "allowed_categories": ["news"],
        "default_category": "news",
        "max_categories": 3,
        "max_tags": 6,
        "create_new_tags": True,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Secrets:
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    wp_url: str | None = None
    wp_user: str | None = None
    wp_app_password: str | None = None
    slack_webhook_url: str | None = None
    alert_webhook_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    editor_email: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Secrets":
        e = env if env is not None else os.environ

        def g(name: str) -> str | None:
            value = (e.get(name) or "").strip()
            return value or None

        port = g("SMTP_PORT")
        editors = [x.strip() for x in (g("EDITOR_EMAIL") or "").split(",") if x.strip()]
        return cls(
            anthropic_api_key=g("ANTHROPIC_API_KEY"),
            openai_api_key=g("OPENAI_API_KEY"),
            wp_url=g("WP_URL"),
            wp_user=g("WP_USER"),
            # WordPress shows app passwords with spaces; they work either way.
            wp_app_password=g("WP_APP_PASSWORD"),
            slack_webhook_url=g("SLACK_WEBHOOK_URL"),
            alert_webhook_url=g("ALERT_WEBHOOK_URL"),
            smtp_host=g("SMTP_HOST"),
            smtp_port=int(port) if port and port.isdigit() else 587,
            smtp_user=g("SMTP_USER"),
            smtp_password=g("SMTP_PASSWORD"),
            smtp_from=g("SMTP_FROM") or g("SMTP_USER"),
            editor_email=editors,
        )


@dataclass
class Config:
    data: dict[str, Any]
    secrets: Secrets
    root: Path = ROOT

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def site_url(self) -> str:
        return (self.secrets.wp_url or self.data["site"]["url"]).rstrip("/")

    @property
    def style_guide(self) -> str:
        path = self.root / "STYLE_GUIDE.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @property
    def state_path(self) -> Path:
        return self.root / "state" / "newsdesk_state.json"

    @property
    def out_dir(self) -> Path:
        path = self.root / "out"
        path.mkdir(parents=True, exist_ok=True)
        return path


def load_config(path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    data = _merge(DEFAULTS, raw)
    return Config(data=data, secrets=Secrets.from_env(env), root=cfg_path.resolve().parent)
