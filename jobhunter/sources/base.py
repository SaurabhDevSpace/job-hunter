"""Shared HTTP plumbing and the Source contract."""
from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any

import requests

from ..models import Job

log = logging.getLogger("jobhunter.sources")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})


def http_get(url: str, *, params: dict[str, Any] | None = None, timeout: int = 25,
             retries: int = 2, headers: dict[str, str] | None = None) -> requests.Response | None:
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt + random.random())
                continue
            log.debug("GET %s -> %s", url, r.status_code)
            return None
        except requests.RequestException as exc:
            if attempt < retries:
                time.sleep(2 ** attempt + random.random())
                continue
            log.debug("GET %s failed: %s", url, exc)
    return None


def http_post(url: str, payload: dict[str, Any], *, timeout: int = 25, retries: int = 1,
              headers: dict[str, str] | None = None) -> requests.Response | None:
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    for attempt in range(retries + 1):
        try:
            r = _session.post(url, json=payload, timeout=timeout, headers=hdrs)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return None
        except requests.RequestException:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
    return None


def get_json(url: str, **kw: Any) -> Any | None:
    r = http_get(url, **kw)
    if not r:
        return None
    try:
        return r.json()
    except ValueError:
        return None


class Source(ABC):
    """A job source. `fetch` must never raise — return [] on failure."""

    name: str = "base"
    hidden: bool = False          # True = company-owned ATS ("hidden job market")
    requires_credentials: bool = False

    @abstractmethod
    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        ...

    def safe_fetch(self, matrix: dict[str, Any]) -> tuple[list[Job], str | None]:
        try:
            jobs = [j.normalize() for j in self.fetch(matrix) if j and j.title and j.url]
            return jobs, None
        except Exception as exc:  # noqa: BLE001 - a bad source must not kill the run
            log.warning("source %s failed: %s", self.name, exc, exc_info=True)
            return [], f"{type(exc).__name__}: {exc}"


def matches_any(text: str, seeds: list[str]) -> bool:
    low = (text or "").lower()
    return any(s.lower() in low for s in seeds)
