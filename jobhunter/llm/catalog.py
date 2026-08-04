"""Dynamic OpenRouter free-model discovery and per-task ranking.

No model IDs are hardcoded in the routing path. At runtime we ask OpenRouter which
models are currently free, keep only real text->text chat models, then rank them for
each task (parse / match / resume) using capability heuristics.

Discovery order (first success wins, then cached to disk with a TTL):
  1. https://openrouter.ai/api/frontend/v1/models/find?active=true&fmt=cards&max_output_price=0
     — richest payload: provider, rate limits, supported params, disabled/deranked flags.
  2. https://openrouter.ai/api/v1/models  — official, filtered locally to price == 0.

No fixed model IDs are used. If both discovery endpoints fail and no valid cache exists,
LLM work is skipped while deterministic scoring and offline tailoring remain available.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

log = logging.getLogger("jobhunter.llm.catalog")

FRONTEND_URL = ("https://openrouter.ai/api/frontend/v1/models/find"
                "?active=true&fmt=cards&max_output_price=0")
V1_URL = "https://openrouter.ai/api/v1/models"

_BROWSER_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "referer": "https://openrouter.ai/models?max_output_price=0",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
}

# Models that are free but not usable as general chat/reasoning engines.
_EXCLUDE_SLUG = re.compile(
    r"(embed|embedding|rerank|whisper|tts|speech|voice|audio|lyria|music|"
    r"image|imagen|dall|diffusion|video|veo|sora|clip|hailuo|"
    r"guard|content-safety|moderation|shield|classifier|"
    r"ocr|caption)", re.I)

# Coding/agentic specialists: fine for parsing, weaker for executive prose.
_CODE_SLUG = re.compile(r"(code|coder|codestral|devstral|laguna|swe-|starcoder)", re.I)
_PREVIEW = re.compile(r"(preview|alpha|beta|experimental|exp-|rc\d)", re.I)
_SMALL_FAST = re.compile(r"(flash|nano|mini|lite|small|turbo|instant|fast|air)", re.I)

# Quality priors by author/family. Not model IDs — families, so new versions inherit.
_FAMILY_PRIOR: dict[str, float] = {
    "anthropic": 10.0, "openai": 9.5, "google": 9.0, "deepseek": 8.8, "x-ai": 8.5,
    "qwen": 8.4, "meta-llama": 8.2, "nvidia": 8.0, "mistralai": 7.8, "moonshotai": 7.8,
    "z-ai": 7.5, "cohere": 7.4, "microsoft": 7.2, "amazon": 7.0, "ai21": 6.8,
    "minimax": 6.8, "inclusionai": 6.5, "baidu": 6.3, "tencent": 6.3, "openrouter": 6.0,
    "poolside": 6.0, "liquid": 5.8, "arcee-ai": 5.5, "agentica-org": 5.2,
    "tngtech": 5.2, "sarvamai": 5.0, "rekaai": 5.0, "featherless": 4.5,
}

# Per-task weighting of the capability signals.
TASK_WEIGHTS: dict[str, dict[str, float]] = {
    # Fast, high-volume structured extraction. JSON-mode support matters most.
    "parse":  {"family": 1.0, "size": 0.8, "context": 1.2, "json": 4.0, "reasoning": 0.4,
               "output_len": 0.4, "speed": 1.6, "code_penalty": -0.5, "limits": 1.0},
    # Deep candidate-to-role reasoning. Reasoning + scale matter most.
    "match":  {"family": 1.6, "size": 2.0, "context": 1.0, "json": 2.5, "reasoning": 3.0,
               "output_len": 0.8, "speed": -0.4, "code_penalty": -2.0, "limits": 0.8},
    # Long-form ATS resume synthesis. Needs scale, long output, prose quality.
    "resume": {"family": 1.8, "size": 2.2, "context": 1.2, "json": 2.5, "reasoning": 2.4,
               "output_len": 1.8, "speed": -0.6, "code_penalty": -2.5, "limits": 0.6},
}


@dataclass
class ModelCard:
    """A normalized, currently-free OpenRouter chat model."""
    id: str                       # exact id to send as `model` (may end in ':free')
    slug: str
    name: str = ""
    author: str = ""
    context_length: int = 0
    max_output_tokens: int = 0
    supports_reasoning: bool = False
    supports_response_format: bool = False
    supports_structured_outputs: bool = False
    supports_tools: bool = False
    limit_rpd: int = 0            # 0 = not advertised
    limit_rpm: int = 0
    provider: str = ""
    deranked: bool = False
    params_b: float = 0.0         # parameter count parsed from the slug
    source: str = "frontend"
    raw_scores: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------ scoring
    def score_for(self, task: str) -> float:
        w = TASK_WEIGHTS.get(task, TASK_WEIGHTS["match"])
        family = _FAMILY_PRIOR.get(self.author.lower(), 5.0)
        size = math.log2(self.params_b + 2) if self.params_b else 4.0
        context = math.log10(max(self.context_length, 1000)) - 3.0        # 0 @ 1k, 3 @ 1M
        json_cap = (2.0 if self.supports_structured_outputs else 0.0) + \
                   (1.5 if self.supports_response_format else 0.0) + \
                   (0.5 if self.supports_tools else 0.0)
        reasoning = 1.0 if self.supports_reasoning else 0.0
        output_len = math.log2(max(self.max_output_tokens, 1024) / 1024)  # 0 @ 1k, 6 @ 64k
        speed = 1.0 if _SMALL_FAST.search(self.slug) else 0.0
        code_pen = 1.0 if _CODE_SLUG.search(self.slug) else 0.0
        # Advertised throughput headroom (unknown -> neutral 0.5).
        limits = 0.5 if not (self.limit_rpd or self.limit_rpm) else min(
            1.0, (self.limit_rpd or 0) / 1000 + (self.limit_rpm or 0) / 60)

        total = (w["family"] * family + w["size"] * size + w["context"] * context
                 + w["json"] * json_cap + w["reasoning"] * reasoning
                 + w["output_len"] * output_len + w["speed"] * speed
                 + w["code_penalty"] * code_pen + w["limits"] * limits)
        if _PREVIEW.search(self.slug):
            total -= 2.0
        if self.deranked:
            total -= 6.0
        self.raw_scores[task] = round(total, 3)
        return total

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["raw_scores"] = dict(self.raw_scores)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelCard":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# ---------------------------------------------------------------------------
def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _params_from_slug(slug: str) -> float:
    """Best-effort parameter count in billions, e.g. '...-550b-a55b' -> 550."""
    matches = re.findall(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*b(?![a-z0-9])", slug.lower())
    vals = [float(m) for m in matches if 0.3 <= float(m) <= 3000]
    return max(vals) if vals else 0.0


def _is_text_chat(input_mods: Iterable[str], output_mods: Iterable[str]) -> bool:
    out = {str(m).lower() for m in (output_mods or [])}
    inp = {str(m).lower() for m in (input_mods or [])}
    if "text" not in out or "text" not in inp:
        return False
    # reject anything that also emits non-text (audio/image/video/embeddings)
    return not (out - {"text"})


# ------------------------------------------------------------------- FETCHERS
def _fetch_frontend(timeout: int = 25) -> list[ModelCard]:
    r = requests.get(FRONTEND_URL, headers=_BROWSER_HEADERS, timeout=timeout)
    r.raise_for_status()
    models = ((r.json() or {}).get("data") or {}).get("models") or []
    cards: list[ModelCard] = []
    for m in models:
        ep = m.get("endpoint") or {}
        if not ep or ep.get("is_disabled") or ep.get("is_byok_only") or m.get("hidden"):
            continue
        if ep.get("has_chat_completions") is False:
            continue
        pricing = ep.get("pricing") or {}
        if _num(pricing.get("prompt"), 1) > 0 or _num(pricing.get("completion"), 1) > 0:
            continue
        slug = m.get("slug") or ""
        if not slug or _EXCLUDE_SLUG.search(slug):
            continue
        if not _is_text_chat(m.get("input_modalities") or [], m.get("output_modalities") or []):
            continue
        params = set(ep.get("supported_parameters") or [])
        variant = ep.get("variant") or ""
        model_id = f"{slug}:free" if variant and variant != "standard" else slug
        cards.append(ModelCard(
            id=model_id, slug=slug, name=m.get("name") or slug,
            author=(m.get("author") or slug.split("/")[0] or ""),
            context_length=int(_num(ep.get("context_length") or m.get("context_length"))),
            max_output_tokens=int(_num(ep.get("max_completion_tokens"))),
            supports_reasoning=bool(ep.get("supports_reasoning") or m.get("supports_reasoning")),
            supports_response_format="response_format" in params,
            supports_structured_outputs="structured_outputs" in params,
            supports_tools=bool(ep.get("supports_tool_parameters") or "tools" in params),
            limit_rpd=int(_num(ep.get("limit_rpd") or m.get("limit_rpd"))),
            limit_rpm=int(_num(ep.get("limit_rpm") or m.get("limit_rpm"))),
            provider=ep.get("provider_display_name") or ep.get("provider_name") or "",
            deranked=bool(ep.get("is_deranked")),
            params_b=_params_from_slug(slug), source="frontend",
        ))
    return cards


def _fetch_v1(timeout: int = 25) -> list[ModelCard]:
    r = requests.get(V1_URL, headers={"accept": "application/json"}, timeout=timeout)
    r.raise_for_status()
    cards: list[ModelCard] = []
    for m in (r.json() or {}).get("data") or []:
        pricing = m.get("pricing") or {}
        if _num(pricing.get("prompt"), 1) > 0 or _num(pricing.get("completion"), 1) > 0:
            continue
        mid = m.get("id") or ""
        slug = mid.split(":")[0]
        if not mid or _EXCLUDE_SLUG.search(slug):
            continue
        arch = m.get("architecture") or {}
        if not _is_text_chat(arch.get("input_modalities") or [], arch.get("output_modalities") or []):
            continue
        params = set(m.get("supported_parameters") or [])
        top = m.get("top_provider") or {}
        cards.append(ModelCard(
            id=mid, slug=slug, name=m.get("name") or mid, author=slug.split("/")[0],
            context_length=int(_num(m.get("context_length") or top.get("context_length"))),
            max_output_tokens=int(_num(top.get("max_completion_tokens"))),
            supports_reasoning=bool({"reasoning", "include_reasoning"} & params),
            supports_response_format="response_format" in params,
            supports_structured_outputs="structured_outputs" in params,
            supports_tools="tools" in params,
            params_b=_params_from_slug(slug), source="v1",
        ))
    return cards


# -------------------------------------------------------------------- CATALOG
class ModelCatalog:
    """Discovers, caches and ranks currently-free models."""

    def __init__(self, policy: dict[str, Any], cache_path: Path):
        self.policy = policy or {}
        self.cache_path = Path(cache_path)
        self._cards: list[ModelCard] = []
        self._fetched_at: float = 0.0
        self._origin: str = "none"

    # -- lifecycle ----------------------------------------------------------
    @property
    def ttl_seconds(self) -> float:
        return float(self.policy.get("catalog_ttl_hours", 12)) * 3600

    def cards(self, refresh: bool = False) -> list[ModelCard]:
        if self._cards and not refresh and (time.time() - self._fetched_at) < self.ttl_seconds:
            return self._cards
        if not refresh and self._load_cache():
            return self._cards
        self._discover()
        return self._cards

    def _load_cache(self) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            blob = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        age = time.time() - float(blob.get("fetched_at", 0))
        if age > self.ttl_seconds:
            return False
        cards = [ModelCard.from_dict(d) for d in blob.get("models", [])]
        if not cards:
            return False
        self._cards, self._fetched_at = cards, float(blob["fetched_at"])
        self._origin = f"cache({blob.get('origin', '?')}, {int(age / 60)}m old)"
        log.info("model catalog: %d free models from %s", len(cards), self._origin)
        return True

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({
                "fetched_at": self._fetched_at, "origin": self._origin,
                "models": [c.to_dict() for c in self._cards],
            }, indent=2), encoding="utf-8")
        except OSError as exc:
            log.debug("could not write model cache: %s", exc)

    def _discover(self) -> None:
        for label, fn in (("frontend", _fetch_frontend), ("v1", _fetch_v1)):
            try:
                cards = fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("model discovery via %s failed: %s", label, exc)
                continue
            cards = self._apply_policy(cards)
            if cards:
                self._cards, self._origin = cards, label
                self._fetched_at = time.time()
                self._save_cache()
                log.info("model catalog: discovered %d free text models via %s", len(cards), label)
                return

        self._cards = []
        self._origin, self._fetched_at = "unavailable", time.time()
        log.warning("model discovery failed everywhere — no dynamic models available")

    def _apply_policy(self, cards: list[ModelCard]) -> list[ModelCard]:
        block = [b.lower() for b in self.policy.get("blocklist", [])]
        min_ctx = int(self.policy.get("min_context_length", 16000))
        out: list[ModelCard] = []
        seen: set[str] = set()
        for c in cards:
            if c.id in seen:
                continue
            if any(b in c.id.lower() for b in block):
                continue
            if c.context_length and c.context_length < min_ctx:
                continue
            seen.add(c.id)
            out.append(c)
        return out

    # -- ranking ------------------------------------------------------------
    def ranked(self, task: str, refresh: bool = False) -> list[ModelCard]:
        cards = list(self.cards(refresh=refresh))
        if self.policy.get("require_json_mode_for_parse", True) and task == "parse":
            json_capable = [c for c in cards if c.supports_response_format
                            or c.supports_structured_outputs]
            if json_capable:
                # JSON-mode models first, the rest kept as deep fallbacks
                rest = [c for c in cards if c not in json_capable]
                json_capable.sort(key=lambda c: -c.score_for(task))
                rest.sort(key=lambda c: -c.score_for(task))
                return json_capable + rest
        cards.sort(key=lambda c: -c.score_for(task))
        return cards

    def chain(self, task: str, limit: int | None = None, refresh: bool = False) -> list[str]:
        """Ordered model IDs for a task: pinned first, then dynamically ranked."""
        pinned = [m for m in (self.policy.get("pinned_models") or {}).get(task, []) if m]
        ranked = [c.id for c in self.ranked(task, refresh=refresh)]
        n = limit if limit is not None else int(self.policy.get("chain_length", 5))
        chain, seen = [], set()
        for m in [*pinned, *ranked]:
            if m and m not in seen:
                seen.add(m)
                chain.append(m)
            if len(chain) >= n:
                break
        return chain

    def supports_json_mode(self, model_id: str) -> bool:
        for c in self._cards:
            if c.id == model_id:
                return c.supports_response_format or c.supports_structured_outputs
        return True   # unknown -> try it; a 400 will demote the model anyway

    def card(self, model_id: str) -> ModelCard | None:
        return next((c for c in self._cards if c.id == model_id), None)

    def describe(self, task: str) -> list[dict[str, Any]]:
        return [{
            "id": c.id, "score": round(c.score_for(task), 2), "params_b": c.params_b,
            "context": c.context_length, "max_out": c.max_output_tokens,
            "reasoning": c.supports_reasoning,
            "json": c.supports_response_format or c.supports_structured_outputs,
            "provider": c.provider, "source": c.source,
        } for c in self.ranked(task)]

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def fetched_at(self) -> float:
        return self._fetched_at
