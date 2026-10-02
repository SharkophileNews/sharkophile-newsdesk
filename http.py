"""Thin HTTP layer. Everything that talks to the network goes through
`HttpClient`, so tests can swap in a fake."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import requests

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = (
    f"SharkophileNewsdesk/{__version__} "
    "(+https://www.sharkophile.com; news@sharkophile.com)"
)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 529}


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class HttpClient:
    """requests.Session with retries, timeouts and a descriptive user agent."""

    def __init__(self, retries: int = 3, timeout: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.retries = retries
        self.timeout = timeout
        self.sleep = sleep

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        for attempt in range(self.retries + 1):
            resp: requests.Response | None = None
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                log.warning("%s %s failed (%s), attempt %d", method, url, exc, attempt + 1)
                if attempt == self.retries:
                    raise HttpError(f"{method} {url} failed: {exc}") from exc
            else:
                if resp.status_code not in RETRY_STATUSES or attempt == self.retries:
                    return resp
                log.warning("%s %s -> %s, retrying", method, url, resp.status_code)
            delay = min(60.0, (2 ** attempt) * 1.5 + random.random())
            retry_after = resp.headers.get("retry-after") if resp is not None else None
            if retry_after and retry_after.isdigit():
                delay = min(120.0, float(retry_after))
            self.sleep(delay)
        raise HttpError(f"{method} {url} failed after retries")  # pragma: no cover

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)
