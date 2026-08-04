"""Cline free-model fallback for when OpenRouter runs out of capacity.

OpenRouter's free tier is hard-capped (~50 requests/day) and hitting that
ceiling mid-run stalls onboarding and deep searches. Cline's inference API
fronts a small rotating set of genuinely-free models that can absorb the
overflow, so this module exists purely as a safety net behind
`OpenRouterClient` — it is never the primary provider (NVIDIA is tried
first, then OpenRouter, and Cline only when both of those are exhausted).

This app has no admin panel/accounts store, so settings are env-var driven
(`CLINE_API_KEY`, `CLINE_MODELS`, `CLINE_DAILY_CAP`) with the same
verified-working defaults used by the sibling All-in-One Job Hunter app.

Notable differences from OpenRouter that this client absorbs:

* Responses are wrapped: ``{"data": {...OpenAI body...}, "success": bool}``.
* Failures sometimes arrive as HTTP 200 with ``success: false``.
* There is **no model-catalog endpoint** for API keys (``GET /api/v1/models``
  is a genuine 404 with valid auth), and the free line-up rotates, so the
  usable model list is a maintained constant rather than discovered like
  OpenRouter's.

Otherwise the payload is OpenAI-shaped: ``system`` role, ``temperature`` and
``response_format={"type": "json_object"}`` are all accepted, and unknown
params are tolerated.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from ..config import DB_PATH

log = logging.getLogger("jobhunter.llm.cline")

BASE_URL = "https://api.cline.bot/api/v1"

# Verified free (cost 0, is_byok) *and* reliably strict-JSON capable; other
# UI-advertised "free" models either bill or are blocked outside Cline's editor.
CLINE_DEFAULT_MODELS = ["poolside/laguna-s-2.1:free", "stepfun/step-3.7-flash"]
CLINE_DEFAULT_DAILY_CAP = 100

# Own ledger table so Cline usage can never be confused with, or accounted
# against, the OpenRouter request budget.
SCHEMA = """
CREATE TABLE IF NOT EXISTS cline_ledger (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""


def api_key() -> str:
    return os.getenv("CLINE_API_KEY", "").strip()


def _models() -> list[str]:
    raw = os.getenv("CLINE_MODELS", "").strip()
    if not raw:
        return list(CLINE_DEFAULT_MODELS)
    return [m.strip() for m in raw.split(",") if m.strip()] or list(CLINE_DEFAULT_MODELS)


def _daily_cap() -> int:
    raw = os.getenv("CLINE_DAILY_CAP", "").strip()
    try:
        return int(raw) if raw else CLINE_DEFAULT_DAILY_CAP
    except ValueError:
        return CLINE_DEFAULT_DAILY_CAP


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class ClineUnavailable(RuntimeError):
    """Fallback cannot be used (unconfigured, capped, or no models)."""


class ClineClient:
    """Minimal client mirroring the slice of OpenRouterClient the pipeline uses."""

    def __init__(self, *, db_path_override: Path | str | None = None,
                 logger: Callable[..., None] | None = None):
        self.key = api_key()
        self.model_list = _models()
        self.cap = _daily_cap()
        self.db = str(db_path_override or DB_PATH)
        self._session = requests.Session()
        self._call_logger = logger
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------------- state
    @property
    def models(self) -> list[str]:
        return list(self.model_list)

    @property
    def daily_cap(self) -> int:
        return int(self.cap)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    @property
    def enabled(self) -> bool:
        return self.configured and bool(self.model_list)

    def used_today(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT calls FROM cline_ledger WHERE day=?", (_today(),)).fetchone()
        return int(row["calls"]) if row else 0

    def _spend(self, n: int = 1) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO cline_ledger (day, calls) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET calls = calls + excluded.calls",
                (_today(), n),
            )
            c.commit()

    def state(self) -> dict[str, Any]:
        used = self.used_today()
        cap = self.daily_cap
        return {
            "configured": self.configured,
            "usable": self.enabled,
            "models": self.models,
            "daily_cap": cap,
            "used_today": used,
            "remaining_today": max(0, cap - used),
        }

    def available(self) -> bool:
        """True when a fallback call could actually be made right now."""
        return self.enabled and (self.daily_cap <= 0 or self.used_today() < self.daily_cap)

    # ------------------------------------------------------------------ call
    def chat(self, task: str, messages: list[dict[str, str]], *,
             response_format_json: bool = False,
             max_tokens: int | None = None,
             temperature: float | None = None,
             timeout: int = 180,
             max_attempts_per_model: int = 2) -> tuple[str, str, dict[str, Any]]:
        """Return ``(text, model_used, usage)``; raise on total failure.

        Deliberately returns primitives rather than an ``LLMResult`` so this
        module stays import-free of the OpenRouter client (which imports *it*).
        """
        if not self.configured:
            raise ClineUnavailable("CLINE_API_KEY is not set.")
        if not self.model_list:
            raise ClineUnavailable("No Cline models configured.")

        cap = self.daily_cap
        errors: list[str] = []

        for model in self.model_list:
            for attempt in range(1, max(1, max_attempts_per_model) + 1):
                # Re-check inside the loop: a long run must not blow past the cap
                # just because it was under it when the call started.
                if cap > 0 and self.used_today() >= cap:
                    raise ClineUnavailable(f"Cline daily cap reached ({cap} calls).")

                payload: dict[str, Any] = {"model": model, "messages": messages}
                if max_tokens:
                    payload["max_tokens"] = int(max_tokens)
                if temperature is not None:
                    payload["temperature"] = float(temperature)
                if response_format_json:
                    payload["response_format"] = {"type": "json_object"}

                t0 = time.monotonic()
                err = ""
                try:
                    self._spend()  # count the attempt before it can fail, like the budget guard
                    r = self._session.post(
                        f"{BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {self.key}",
                                 "Content-Type": "application/json"},
                        json=payload, timeout=timeout)
                    latency = int((time.monotonic() - t0) * 1000)

                    body: dict[str, Any] = {}
                    try:
                        body = r.json() or {}
                    except ValueError:
                        body = {}

                    if r.status_code == 200 and body.get("success"):
                        data = body.get("data") or {}
                        text = _extract_text(data)
                        if text.strip():
                            log.info("[%s] cline fallback ok via %s (%dms)", task, model, latency)
                            self._log(task, model, True, latency)
                            return text, model, (data.get("usage") or {})
                        err = "empty content"
                    else:
                        err = f"HTTP {r.status_code}: {_error_text(body) or r.text[:200]}"
                except requests.RequestException as exc:
                    latency = int((time.monotonic() - t0) * 1000)
                    err = f"{type(exc).__name__}: {exc}"

                self._log(task, model, False, latency, err)
                errors.append(f"{model}: {err}")
                log.warning("[%s] cline %s attempt %d failed: %s", task, model, attempt, err)
                if attempt < max_attempts_per_model:
                    time.sleep(2)

        raise ClineUnavailable(
            f"All {len(self.model_list)} Cline model(s) failed for '{task}': {errors[-3:]}")

    def _log(self, task: str, model: str, ok: bool, latency_ms: int, error: str = "") -> None:
        if self._call_logger:
            try:
                self._call_logger(task, f"cline:{model}", ok, latency_ms, error)
            except Exception:
                log.debug("cline call logging failed", exc_info=True)


def _extract_text(data: dict[str, Any]) -> str:
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not str(content).strip():
        content = msg.get("reasoning") or ""
    return str(content or "")


def _error_text(body: dict[str, Any]) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        err = err.get("message") or str(err)
    return str(err or "")[:220]
