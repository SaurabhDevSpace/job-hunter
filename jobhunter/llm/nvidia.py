"""NVIDIA (build.nvidia.com / integrate.api.nvidia.com) primary provider.

Tried *before* OpenRouter (and Cline) — NVIDIA's hosted NIM catalog is not
rate-capped the way OpenRouter's free tier is, so it is the first attempt
rather than a rescue-only fallback. Kept as a separate, defensive module
(mirroring `cline.py`) so a broken NVIDIA account can never block the
existing OpenRouter -> Cline chain: any failure here just raises
`NvidiaUnavailable` and the caller moves on.

This app has no admin panel/accounts store, so settings are env-var driven
(`NVIDIA_API_KEY`, `NVIDIA_MODELS`, `NVIDIA_DAILY_CAP`) with the same
verified-working defaults used by the sibling All-in-One Job Hunter app.

Notable differences from OpenRouter that this client absorbs:

* Plain OpenAI-shaped payload/response (`choices[0].message.content`), no
  envelope wrapping like Cline's `{"data": ..., "success": ...}`.
* Several models (nemotron-nano, step-3.7-flash, etc.) are "reasoning"
  models that can return `content: null` with the actual text sitting in a
  `reasoning`/`reasoning_content` field instead — handled by `_extract_text`.
* Some catalog entries are currently flaky (529 overloaded / 502 bad
  gateway) independent of account access — this is why the client retries
  across the *whole* model list rather than giving up on the first error.
* `deepseek-ai/deepseek-v4-pro` is deliberately excluded from the defaults:
  verified live to hang for the full request timeout instead of failing
  fast, which would stall every call that reached it.
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

log = logging.getLogger("jobhunter.llm.nvidia")

BASE_URL = "https://integrate.api.nvidia.com/v1"

# Verified live against the deployed key — each of these actually returned a
# completion. `deepseek-v4-pro` excluded: it hangs for the full timeout
# instead of failing fast (see module docstring).
NVIDIA_DEFAULT_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3-super-120b-a12b",
    "openai/gpt-oss-20b",
]
NVIDIA_DEFAULT_DAILY_CAP = 300
RETIRED_MODELS = {
    "deepseek-ai/deepseek-v4-flash",
    "stepfun-ai/step-3.7-flash",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "meta/llama-3.1-70b-instruct",
}
PERMANENT_CLIENT_ERRORS = {400, 401, 403, 404, 410, 422}

# Own ledger table so NVIDIA usage can never be confused with, or accounted
# against, the OpenRouter budget.
SCHEMA = """
CREATE TABLE IF NOT EXISTS nvidia_ledger (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""


def api_key() -> str:
    return os.getenv("NVIDIA_API_KEY", "").strip()


def _models() -> list[str]:
    raw = os.getenv("NVIDIA_MODELS", "").strip()
    configured = [m.strip() for m in raw.split(",") if m.strip()] if raw else NVIDIA_DEFAULT_MODELS
    # A previously deployed NVIDIA_MODELS value can outlive a retired catalog
    # entry. Filter it here rather than making every request pay for its 410.
    return [model for model in configured if model not in RETIRED_MODELS]


def _daily_cap() -> int:
    raw = os.getenv("NVIDIA_DAILY_CAP", "").strip()
    try:
        return int(raw) if raw else NVIDIA_DEFAULT_DAILY_CAP
    except ValueError:
        return NVIDIA_DEFAULT_DAILY_CAP


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class NvidiaUnavailable(RuntimeError):
    """Provider cannot be used (unconfigured, capped, or no models)."""


class NvidiaClient:
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
            row = c.execute("SELECT calls FROM nvidia_ledger WHERE day=?", (_today(),)).fetchone()
        return int(row["calls"]) if row else 0

    def _spend(self, n: int = 1) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO nvidia_ledger (day, calls) VALUES (?, ?) "
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
        """True when a call could actually be made right now."""
        return self.enabled and (self.daily_cap <= 0 or self.used_today() < self.daily_cap)

    # ------------------------------------------------------------------ call
    def chat(self, task: str, messages: list[dict[str, str]], *,
             response_format_json: bool = False,
             max_tokens: int | None = None,
             temperature: float | None = None,
             timeout: int = 45,
             max_attempts_per_model: int = 2) -> tuple[str, str, dict[str, Any]]:
        """Return ``(text, model_used, usage)``; raise on total failure.

        Deliberately returns primitives rather than an ``LLMResult`` so this
        module stays import-free of the OpenRouter client (which imports *it*).
        """
        if not self.configured:
            raise NvidiaUnavailable("NVIDIA_API_KEY is not set.")
        if not self.model_list:
            raise NvidiaUnavailable("No NVIDIA models configured.")

        cap = self.daily_cap
        errors: list[str] = []
        # Reasoning models can burn most of the token budget on <think>-style
        # reasoning before ever emitting `content`; make sure there is enough
        # headroom for an actual answer to follow the reasoning.
        effective_max_tokens = max(int(max_tokens or 0), 512)

        models = _prioritize_models(self.model_list, task)
        for model in models:
            for attempt in range(1, max(1, max_attempts_per_model) + 1):
                # Re-check inside the loop: a long run must not blow past the
                # cap just because it was under it when the call started.
                if cap > 0 and self.used_today() >= cap:
                    raise NvidiaUnavailable(f"NVIDIA daily cap reached ({cap} calls).")

                payload: dict[str, Any] = {
                    "model": model, "messages": messages,
                    "max_tokens": effective_max_tokens,
                }
                if temperature is not None:
                    payload["temperature"] = float(temperature)
                if response_format_json:
                    payload["response_format"] = {"type": "json_object"}

                t0 = time.monotonic()
                err = ""
                status_code: int | None = None
                try:
                    self._spend()  # count the attempt before it can fail, like the budget guard
                    r = self._session.post(
                        f"{BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {self.key}",
                                 "Content-Type": "application/json"},
                        json=payload, timeout=timeout)
                    latency = int((time.monotonic() - t0) * 1000)
                    status_code = r.status_code

                    body: dict[str, Any] = {}
                    try:
                        body = r.json() or {}
                    except ValueError:
                        body = {}

                    if r.status_code == 200 and "choices" in body:
                        text = _extract_text(body)
                        if text.strip():
                            log.info("[%s] nvidia ok via %s (%dms)", task, model, latency)
                            self._log(task, model, True, latency)
                            return text, model, (body.get("usage") or {})
                        err = "empty content"
                    else:
                        err = f"HTTP {r.status_code}: {_error_text(body) or r.text[:200]}"
                except requests.RequestException as exc:
                    latency = int((time.monotonic() - t0) * 1000)
                    err = f"{type(exc).__name__}: {exc}"

                self._log(task, model, False, latency, err)
                errors.append(f"{model}: {err}")
                log.warning("[%s] nvidia %s attempt %d failed: %s", task, model, attempt, err)
                # Authentication, validation, and retired-model failures cannot
                # improve on a retry. Move straight to the next provider model.
                if status_code in PERMANENT_CLIENT_ERRORS:
                    break
                if attempt < max_attempts_per_model:
                    time.sleep(2)

        raise NvidiaUnavailable(
            f"All {len(self.model_list)} NVIDIA model(s) failed for '{task}': {errors[-3:]}")

    def _log(self, task: str, model: str, ok: bool, latency_ms: int, error: str = "") -> None:
        if self._call_logger:
            try:
                self._call_logger(task, f"nvidia:{model}", ok, latency_ms, error)
            except Exception:
                log.debug("nvidia call logging failed", exc_info=True)


def _prioritize_models(models: list[str], task: str) -> list[str]:
    """Prefer the strongest models for one-time profile/strategy decisions."""
    if task not in {"profile", "strategy"}:
        return list(models)
    preferred = ("nemotron-3-ultra-550b", "nemotron-3-super-120b", "deepseek-v4")
    return sorted(models, key=lambda model: next(
        (i for i, marker in enumerate(preferred) if marker in model), len(preferred)))


def _extract_text(body: dict[str, Any]) -> str:
    try:
        msg = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not str(content).strip():
        # Reasoning-style models (nemotron-nano, step-3.7-flash, ...) can put
        # the actual answer in `reasoning`/`reasoning_content` when `content`
        # comes back null.
        content = msg.get("reasoning") or msg.get("reasoning_content") or ""
    return str(content or "")


def _error_text(body: dict[str, Any]) -> str:
    err = body.get("error") or body.get("detail") or body.get("title")
    if isinstance(err, dict):
        err = err.get("message") or str(err)
    return str(err or "")[:220]
