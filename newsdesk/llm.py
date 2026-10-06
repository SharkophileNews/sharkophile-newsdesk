"""Minimal Claude Messages API client (no SDK dependency).

- `json()` asks for JSON matching a schema via `output_config.format`
  (structured outputs), falling back to prompt-only JSON if the API rejects it.
- `research()` runs the server-side web search tool and returns notes plus the
  URLs Claude cited."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .http import HttpError

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
WEB_SEARCH_TOOL = "web_search_20260318"


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    calls: int = 0
    by_model: dict[str, list[int]] = field(default_factory=dict)

    def add(self, model: str, usage: dict) -> None:
        i = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0) or 0) \
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        o = int(usage.get("output_tokens", 0))
        self.input_tokens += i
        self.output_tokens += o
        self.calls += 1
        stu = usage.get("server_tool_use") or {}
        self.web_searches += int(stu.get("web_search_requests", 0) or 0)
        row = self.by_model.setdefault(model, [0, 0])
        row[0] += i
        row[1] += o


def _extract_json(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise LLMError("Model did not return JSON")


class Claude:
    def __init__(self, api_key: str | None, http):
        self.api_key = api_key
        self.http = http
        self.usage = Usage()
        self._structured_ok = True

    def _post(self, payload: dict) -> dict:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        resp = self.http.post(API_URL, headers=headers, data=json.dumps(payload), timeout=300)
        if resp.status_code != 200:
            raise HttpError(f"Claude API {resp.status_code}: {resp.text[:500]}",
                            status=resp.status_code, body=resp.text)
        data = resp.json()
        self.usage.add(payload["model"], data.get("usage", {}))
        if data.get("stop_reason") == "refusal":
            raise LLMError("Model declined the request")
        return data

    @staticmethod
    def text_of(data: dict) -> str:
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    def json(self, model: str, system: str, user: str, schema: dict,
             max_tokens: int = 8000) -> dict:
        base = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self._structured_ok:
            payload = dict(base, output_config={"format": {"type": "json_schema", "schema": schema}})
            try:
                data = self._post(payload)
                if data.get("stop_reason") == "max_tokens":
                    raise LLMError("Response hit max_tokens before the JSON was complete")
                return _extract_json(self.text_of(data))
            except HttpError as exc:
                if exc.status == 400 and ("output_config" in exc.body or "format" in exc.body
                                          or "schema" in exc.body):
                    log.warning("Structured outputs rejected (%s); falling back to prompt JSON",
                                exc.body[:200])
                    self._structured_ok = False
                else:
                    raise
        payload = dict(base)
        payload["system"] = (system + "\n\nRespond with ONLY a JSON object matching this JSON "
                             "Schema, no prose, no code fences:\n" + json.dumps(schema))
        data = self._post(payload)
        return _extract_json(self.text_of(data))

    def research(self, model: str, system: str, user: str, max_searches: int = 3,
                 max_tokens: int = 6000) -> tuple[str, list[dict]]:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_searches}],
        }
        data = self._post(payload)
        notes, cited = [], []

        def add(url: str, title: str = "", cited_text: str = "") -> None:
            if url and url not in [x["url"] for x in cited]:
                cited.append({"url": url, "title": title, "cited_text": cited_text})

        for block in data.get("content", []):
            if block.get("type") == "web_search_tool_result":
                # Every page the search actually returned is a real URL the writer may link.
                for r in block.get("content") or []:
                    if isinstance(r, dict) and r.get("type") == "web_search_result":
                        add(r.get("url", ""), r.get("title", ""))
                continue
            if block.get("type") != "text":
                continue
            notes.append(block.get("text", ""))
            for c in block.get("citations") or []:
                add(c.get("url", ""), c.get("title", ""), c.get("cited_text", ""))
                # keep the quoted snippet when a result URL was added before it was cited
                for x in cited:
                    if x["url"] == c.get("url") and not x["cited_text"]:
                        x["cited_text"] = c.get("cited_text", "")
        return "".join(notes).strip(), cited
