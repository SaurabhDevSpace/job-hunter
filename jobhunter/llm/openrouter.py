"""OpenRouter dynamic model router — discovery-driven, budget-aware, cache-first.

Request lifecycle
-----------------
1. Cache lookup (content-addressed on task + messages + params). A hit costs 0 requests.
2. NVIDIA is tried first (see `nvidia.py`) — not rate-capped the way OpenRouter's free
   tier is, so a successful NVIDIA call short-circuits the whole OpenRouter path.
3. Budget check: daily ledger, per-run cap, per-task reservations, client-side RPM guard.
   If OpenRouter's budget/capacity is exhausted, Cline (see `cline.py`) is tried next.
4. The model chain is resolved *at call time* from the live free-model catalog, ranked
   for the task. Models in 429 cooldown are pushed to the back rather than dropped.
5. Per-model retries with exponential backoff on retryable statuses; on a hard failure
   the next model in the chain is tried. `response_format` is only sent to models whose
   advertised capabilities include it, and is dropped automatically on a 400/404/422.
6. Successful completions are written back to the cache.

Provider order: NVIDIA -> OpenRouter -> Cline.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from ..config import DB_PATH, ModelConfig, load_model_config
from .budget import BudgetGuard, ReliabilityLedger, ResponseCache

log = logging.getLogger("jobhunter.llm")

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


class LLMError(RuntimeError):
    """Raised when every model in the chain fails."""


class LLMBudgetError(LLMError):
    """Raised when the request budget is exhausted (distinct from a model failure)."""


@dataclass
class LLMResult:
    text: str
    model: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    cached: bool = False


class OpenRouterClient:
    def __init__(self, config: ModelConfig | None = None,
                 logger: Callable[..., None] | None = None,
                 db_path: Path | str | None = None):
        self.cfg = config or load_model_config()
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._cooldowns: dict[str, float] = {}
        self._no_json_mode: set[str] = set()   # models that rejected response_format
        self._bad_json: set[str] = set()       # models that emit prose instead of JSON
        self._call_logger = logger
        db = str(db_path or DB_PATH)
        self.db_path = db
        policy = self.cfg.budget
        self.budget = BudgetGuard(db, policy, api_key=self.cfg.api_key)
        self.cache = ResponseCache(db, ttl_hours=float(policy.get("cache_ttl_hours", 336)),
                                   enabled=bool(policy.get("cache_enabled", True)))
        self.reliability = ReliabilityLedger(
            db,
            min_samples=int(policy.get("reliability_min_samples", 2)),
            bad_ratio=float(policy.get("reliability_bad_ratio", 0.5)),
        )

    # ------------------------------------------------------------------ setup
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.api_key)

    @property
    def catalog(self):
        return self.cfg.catalog

    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"}
        if self.cfg.site_url:
            h["HTTP-Referer"] = self.cfg.site_url
        if self.cfg.site_name:
            h["X-Title"] = self.cfg.site_name
        return h

    # ------------------------------------------------------------- throttling
    def _throttle(self) -> None:
        min_gap = float(self.cfg.rate_limit.get("min_interval_seconds", 0) or 0)
        if min_gap > 0:
            with self._lock:
                wait = min_gap - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
        self.budget.throttle_rpm()

    # ------------------------------------------------- reasoning / token budget
    def _reasoning_for(self, model: str, task: str) -> dict[str, Any] | None:
        """Constrain chain-of-thought on reasoning models.

        Free reasoning models will happily spend an entire completion budget on
        internal monologue and never reach the JSON. `effort` caps the thinking and
        `exclude` keeps it out of the response body.
        """
        card = self.catalog.card(model)
        if card is not None and not card.supports_reasoning:
            return None
        policy = (self.cfg.raw.get("reasoning") or {})
        cfgd = policy.get(task, policy.get("default"))
        if cfgd is False:
            return {"enabled": False}
        if not cfgd:
            return None
        return dict(cfgd)

    def _max_tokens_for(self, model: str, params: dict[str, Any]) -> int:
        """Give reasoning models headroom so thinking tokens can't starve the answer."""
        base = int(params.get("max_tokens", 2000))
        card = self.catalog.card(model)
        if card is None or not card.supports_reasoning:
            return base
        mult = float(self.cfg.raw.get("reasoning", {}).get("token_multiplier", 3.0))
        want = int(base * mult)
        ceiling = card.max_output_tokens or want
        return max(base, min(want, ceiling))

    def _cooling(self, model: str) -> bool:
        return time.monotonic() < self._cooldowns.get(model, 0.0)

    def _cool_down(self, model: str) -> None:
        secs = float(self.cfg.rate_limit.get("cooldown_seconds_on_429", 60))
        self._cooldowns[model] = time.monotonic() + secs
        log.warning("model %s rate-limited; cooling down %.0fs", model, secs)

    # ------------------------------------------------------------------- core
    def chat(self, task: str, messages: list[dict[str, str]], *,
             response_format_json: bool = False, allow_cache: bool = True,
             exclude_models: set[str] | None = None, **overrides: Any) -> LLMResult:
        params = {**self.cfg.params_for(task), **overrides}

        # 1 --- cache -------------------------------------------------------
        cache_key = ResponseCache.key_for(task, messages, params)
        if allow_cache:
            hit = self.cache.get(cache_key)
            if hit:
                text, model = hit
                log.info("[%s] cache hit (%s) — 0 API calls", task, model or "?")
                return LLMResult(text=text, model=model or "cache", cached=True)

        # 2 --- NVIDIA (tried first) -----------------------------------------
        # NVIDIA's hosted catalog isn't rate-capped the way OpenRouter's free
        # tier is, so it is attempted before OpenRouter rather than only as a
        # rescue. Any failure here is swallowed and falls through to the
        # normal OpenRouter -> Cline chain below.
        nv = self._try_nvidia(task, messages, params, response_format_json,
                              cache_key if allow_cache else "")
        if nv is not None:
            return nv

        if not self.enabled:
            raise LLMError("OPENROUTER_API_KEY is not set — add it to .env to enable LLM features.")

        # 3 --- budget ------------------------------------------------------
        allowed, reason = self.budget.can_spend(task)
        if not allowed:
            fb = self._try_cline(task, messages, params, response_format_json,
                                 cache_key if allow_cache else "", reason)
            if fb is not None:
                return fb
            raise LLMBudgetError(reason)

        retry = self.cfg.retry
        max_attempts = int(retry.get("max_attempts_per_model", 2))
        timeout = int(retry.get("timeout_seconds", 180))
        retry_codes = set(retry.get("retry_status_codes", [429, 500, 502, 503, 504]))

        # 3 --- live, task-ranked model chain -------------------------------
        chain = [m for m in self.cfg.chain_for(task) if m not in (exclude_models or set())]
        if not chain:
            chain = self.cfg.chain_for(task)
        if not chain:
            raise LLMError("no free models available from OpenRouter discovery")
        # Push cooling models and JSON-unreliable models to the back of the chain —
        # demoted, never dropped, so a degraded run can still complete. Unreliability
        # is remembered across processes, so a model that cannot emit JSON stops
        # burning a request on every future run.
        if response_format_json:
            self._bad_json |= self.reliability.unreliable(task)
        chain.sort(key=lambda m: (self._cooling(m), m in self._bad_json))
        attempts: list[dict[str, Any]] = []

        for model in chain:
            want_json = (response_format_json and model not in self._no_json_mode
                         and self.catalog.supports_json_mode(model))
            backoff = float(retry.get("backoff_seconds", 4))
            attempt = 0

            while attempt < max_attempts:
                attempt += 1
                ok_budget, reason = self.budget.can_spend(task)
                if not ok_budget:
                    fb = self._try_cline(task, messages, params, response_format_json,
                                         cache_key if allow_cache else "", reason)
                    if fb is not None:
                        return fb
                    raise LLMBudgetError(reason)

                payload: dict[str, Any] = {
                    "model": model, "messages": messages,
                    "max_tokens": self._max_tokens_for(model, params),
                    "temperature": params.get("temperature", 0.2),
                }
                reasoning = self._reasoning_for(model, task)
                if reasoning:
                    payload["reasoning"] = reasoning
                if want_json:
                    payload["response_format"] = {"type": "json_object"}

                self._throttle()
                t0 = time.monotonic()
                err = ""
                try:
                    self.budget.spend(task)
                    r = self._session.post(f"{self.cfg.base_url}/chat/completions",
                                           headers=self._headers(), json=payload, timeout=timeout)
                    latency = int((time.monotonic() - t0) * 1000)

                    if r.status_code == 200:
                        body = r.json()
                        text = _extract_text(body)
                        if text.strip():
                            self._log(task, model, True, latency)
                            attempts.append({"model": model, "attempt": attempt, "ok": True})
                            if allow_cache:
                                self.cache.put(cache_key, task, model, text)
                            return LLMResult(text=text, model=model, attempts=attempts,
                                             usage=body.get("usage", {}) or {})
                        err = "empty content"
                    else:
                        err = f"HTTP {r.status_code}: {r.text[:200]}"
                        if r.status_code == 429:
                            self._cool_down(model)
                        elif r.status_code in (400, 404, 422) and want_json:
                            # advertised JSON mode but rejected it — retry without it
                            self._no_json_mode.add(model)
                            want_json = False
                            attempt -= 1   # this attempt does not count against the model
                            log.info("[%s] %s rejected response_format; retrying plain", task, model)
                            self._log(task, model, False, latency, err)
                            attempts.append({"model": model, "error": err})
                            continue
                        if r.status_code not in retry_codes:
                            self._log(task, model, False, latency, err)
                            attempts.append({"model": model, "attempt": attempt, "error": err})
                            break
                except requests.RequestException as exc:
                    latency = int((time.monotonic() - t0) * 1000)
                    err = f"{type(exc).__name__}: {exc}"

                self._log(task, model, False, latency, err)
                attempts.append({"model": model, "attempt": attempt, "error": err})
                log.warning("[%s] %s attempt %d failed: %s", task, model, attempt, err)
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= float(retry.get("backoff_multiplier", 2.0))

        # Every model failed. If that was purely rate limiting, OpenRouter is out
        # of capacity rather than broken — exactly the case Cline exists to cover.
        errs = [str(a.get("error", "")) for a in attempts if a.get("error")]
        if errs and all("429" in e for e in errs):
            fb = self._try_cline(task, messages, params, response_format_json,
                                 cache_key if allow_cache else "",
                                 "every OpenRouter model is rate-limited")
            if fb is not None:
                return fb

        raise LLMError(f"All {len(chain)} free models exhausted for task '{task}'. "
                       f"Last attempts: {attempts[-3:]}")

    def _try_cline(self, task: str, messages: list[dict[str, str]],
                   params: dict[str, Any], response_format_json: bool,
                   cache_key: str, reason: str) -> LLMResult | None:
        """Best-effort overflow to Cline's free models. Returns None if the
        fallback isn't usable, so callers keep their original error path."""
        try:
            from .cline import ClineClient, ClineUnavailable
        except Exception:  # pragma: no cover - defensive import guard
            return None

        try:
            client = ClineClient(db_path_override=self.db_path, logger=self._call_logger)
            if not client.available():
                return None
            log.warning("[%s] OpenRouter unavailable (%s) — falling back to Cline", task, reason)
            text, model, usage = client.chat(
                task, messages,
                response_format_json=response_format_json,
                max_tokens=params.get("max_tokens"),
                temperature=params.get("temperature"),
                timeout=int(self.cfg.retry.get("timeout_seconds", 180)),
                max_attempts_per_model=int(self.cfg.retry.get("max_attempts_per_model", 2)),
            )
        except ClineUnavailable as exc:
            log.warning("[%s] Cline fallback unavailable: %s", task, exc)
            return None
        except Exception:
            log.warning("[%s] Cline fallback errored", task, exc_info=True)
            return None

        tagged = f"cline:{model}"
        if cache_key:
            self.cache.put(cache_key, task, tagged, text)
        return LLMResult(text=text, model=tagged, usage=usage,
                         attempts=[{"model": tagged, "ok": True, "provider": "cline"}])

    def _try_nvidia(self, task: str, messages: list[dict[str, str]],
                    params: dict[str, Any], response_format_json: bool,
                    cache_key: str) -> LLMResult | None:
        """Best-effort primary attempt via NVIDIA. Returns None if the
        provider isn't usable/fails, so the caller falls through to the
        normal OpenRouter -> Cline chain unaffected."""
        try:
            from .nvidia import NvidiaClient, NvidiaUnavailable
        except Exception:  # pragma: no cover - defensive import guard
            return None

        try:
            client = NvidiaClient(db_path_override=self.db_path, logger=self._call_logger)
            if not client.available():
                return None
            text, model, usage = client.chat(
                task, messages,
                response_format_json=response_format_json,
                max_tokens=params.get("max_tokens"),
                temperature=params.get("temperature"),
                timeout=int(self.cfg.retry.get("timeout_seconds", 180)),
                max_attempts_per_model=int(self.cfg.retry.get("max_attempts_per_model", 2)),
            )
        except NvidiaUnavailable as exc:
            log.info("[%s] NVIDIA unavailable: %s", task, exc)
            return None
        except Exception:
            log.warning("[%s] NVIDIA attempt errored", task, exc_info=True)
            return None

        tagged = f"nvidia:{model}"
        if cache_key:
            self.cache.put(cache_key, task, tagged, text)
        log.info("[%s] served by NVIDIA (%s) — OpenRouter not touched", task, model)
        return LLMResult(text=text, model=tagged, usage=usage,
                         attempts=[{"model": tagged, "ok": True, "provider": "nvidia"}])

    def json_call(self, task: str, system: str, user: str, *,
                  schema_hint: str = "", repair: bool = True, **overrides: Any) -> tuple[Any, str]:
        """Return (parsed_json, model_used).

        A repair round-trip costs another request, so it is only attempted when the
        budget genuinely allows it.
        """
        sys_msg = system if not schema_hint else \
            f"{system}\n\nReturn ONLY valid JSON matching:\n{schema_hint}"
        messages = [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}]

        tried: set[str] = set()
        result = self.chat(task, messages, response_format_json=True, **overrides)
        parsed = extract_json(result.text)
        if parsed is not None:
            if not result.cached:
                self.reliability.record(result.model, task, ok=True)
            return parsed, result.model

        # This model talks instead of answering — demote it and hand the same prompt
        # to the NEXT model in the chain rather than re-asking the same one.
        self._bad_json.add(result.model)
        tried.add(result.model)
        self.reliability.record(result.model, task, bad_json=True)
        log.info("[%s] %s returned prose, not JSON — demoting and failing over", task, result.model)

        max_failover = int(self.cfg.raw.get("retry", {}).get("json_failover_models", 2))
        for _ in range(max_failover):
            can_spend, _reason = self.budget.can_spend(task)
            if not (repair and can_spend):
                break
            try:
                nxt = self.chat(task, messages, response_format_json=True,
                                exclude_models=tried, allow_cache=False, **overrides)
            except LLMError:
                break
            parsed = extract_json(nxt.text)
            if parsed is not None:
                self.reliability.record(nxt.model, task, ok=True)
                return parsed, nxt.model
            self._bad_json.add(nxt.model)
            tried.add(nxt.model)
            self.reliability.record(nxt.model, task, bad_json=True)

        raise LLMError(f"No free model produced valid JSON for task '{task}' "
                       f"(tried {', '.join(sorted(tried)) or 'none'}).")

    # -------------------------------------------------------------- telemetry
    def budget_state(self) -> dict[str, Any]:
        st = self.budget.state().to_dict()
        st["cache"] = self.cache.stats()
        st["key_info"] = self.budget.detect_limits()
        st["reliability"] = self.reliability.report()
        return st

    def _log(self, task: str, model: str, ok: bool, latency_ms: int, error: str = "") -> None:
        if self._call_logger:
            try:
                self._call_logger(task, model, ok, latency_ms, error)
            except Exception:
                log.debug("llm call logging failed", exc_info=True)


# --------------------------------------------------------------------- helpers
def _extract_text(body: dict[str, Any]) -> str:
    try:
        msg = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not str(content).strip():
        content = msg.get("reasoning") or ""
    return str(content or "")


def extract_json(text: str) -> Any | None:
    """Best-effort JSON recovery from noisy LLM output."""
    if not text:
        return None
    cleaned = _THINK.sub(" ", text).strip()

    fence = _FENCE.search(cleaned)
    candidates = [fence.group(1)] if fence else []
    candidates.append(cleaned)

    for cand in candidates:
        cand = cand.strip()
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = cand.find(opener), cand.rfind(closer)
            if start != -1 and end > start:
                blob = cand[start:end + 1]
                for variant in (blob, re.sub(r",\s*([}\]])", r"\1", blob)):
                    try:
                        return json.loads(variant)
                    except json.JSONDecodeError:
                        continue
    return None
