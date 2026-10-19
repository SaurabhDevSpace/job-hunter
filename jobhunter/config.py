"""Configuration loading: profile ground truth, search matrix, OpenRouter model routing."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:  # optional
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
def data_dir() -> Path:
    return Path(os.getenv("JOBHUNTER_DATA_DIR", ROOT / "data"))


def output_dir() -> Path:
    return data_dir() / "output"


def db_path() -> Path:
    return Path(os.getenv("JOBHUNTER_DB", data_dir() / "jobhunter.sqlite3"))


DATA_DIR = data_dir()
OUTPUT_DIR = output_dir()
DB_PATH = db_path()

load_dotenv(ROOT / ".env")


def _resolve_env(value: Any) -> Any:
    """Expand 'env:VAR' indirections used in models.json."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.getenv(value[4:], "") or ""
    return value


@dataclass
class Competency:
    name: str
    weight: int
    aliases: list[str] = field(default_factory=list)

    @property
    def surface_forms(self) -> list[str]:
        return sorted({self.name.lower(), *(a.lower() for a in self.aliases)}, key=len, reverse=True)


@dataclass
class Profile:
    raw: dict[str, Any]

    # -- identity -----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.raw.get("name", "Candidate")

    @property
    def headline(self) -> str:
        return self.raw.get("headline", "")

    @property
    def summary(self) -> str:
        return " ".join(self.raw.get("summary", "").split())

    @property
    def contact(self) -> dict[str, str]:
        return {
            "email": self.raw.get("email", ""),
            "phone": self.raw.get("phone", ""),
            "location": self.raw.get("location", ""),
            "linkedin": self.raw.get("linkedin", ""),
        }

    # -- structured content -------------------------------------------------
    @property
    def competencies(self) -> list[Competency]:
        return [Competency(**c) for c in self.raw.get("competencies", [])]

    @property
    def experience(self) -> list[dict[str, Any]]:
        return self.raw.get("experience", [])

    @property
    def education(self) -> list[dict[str, Any]]:
        return self.raw.get("education", [])

    @property
    def skills(self) -> dict[str, list[str]]:
        return self.raw.get("skills", {})

    @property
    def certifications(self) -> list[str]:
        return self.raw.get("certifications", [])

    @property
    def achievements(self) -> list[str]:
        return self.raw.get("achievements", [])

    @property
    def seniority(self) -> dict[str, Any]:
        return self.raw.get("seniority", {})

    @property
    def preferences(self) -> dict[str, Any]:
        return self.raw.get("preferences", {})

    @property
    def years_experience(self) -> int:
        raw = self.raw.get("years_experience", 0)
        if isinstance(raw, (int, float)):
            return int(raw)
        # Tolerate human-written values like "4+", "4 years", "~5".
        m = re.search(r"\d+(?:\.\d+)?", str(raw))
        return int(float(m.group())) if m else 0

    def all_bullets(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for role in self.experience:
            for b in role.get("bullets", []):
                out.append({**b, "company": role["company"], "title": role["title"]})
        return out

    def flat_skills(self) -> list[str]:
        return [s for group in self.skills.values() for s in group]

    def as_llm_context(self) -> str:
        """Compact, token-efficient ground-truth block for LLM prompts."""
        lines = [
            f"NAME: {self.name}",
            f"HEADLINE: {self.headline}",
            f"YEARS OF EXPERIENCE: {self.years_experience}",
            f"SUMMARY: {self.summary}",
            "",
            "EDUCATION:",
        ]
        for e in self.education:
            details = str(e.get("details", "")).strip()
            suffix = f" | {details}" if details else ""
            lines.append(f"  - {e['degree']}, {e['institution']} ({e['dates']}){suffix}")
        lines += ["", "EXPERIENCE (factual bullets — do not invent beyond these):"]
        for role in self.experience:
            lines.append(f"  {role['title']} | {role['company']} | {role['dates']}")
            for b in role.get("bullets", []):
                lines.append(f"    * {b['text']}")
        lines += ["", "SKILLS:"]
        for group, items in self.skills.items():
            lines.append(f"  {group}: {', '.join(items)}")
        lines += ["", "CERTIFICATIONS: " + "; ".join(self.certifications)]
        lines += ["", "ACHIEVEMENTS:"] + [f"  - {a}" for a in self.achievements]
        return "\n".join(lines)


MODEL_CATALOG_CACHE = DATA_DIR / "model_catalog.json"


@dataclass
class ModelConfig:
    """Routing *policy*. Actual model IDs are discovered at runtime by ModelCatalog."""
    raw: dict[str, Any]
    _catalog: Any = field(default=None, repr=False, compare=False)

    @property
    def api_key(self) -> str:
        return str(_resolve_env(self.raw.get("openrouter_api_key", "")) or os.getenv("OPENROUTER_API_KEY", ""))

    @property
    def base_url(self) -> str:
        return self.raw.get("base_url", "https://openrouter.ai/api/v1").rstrip("/")

    @property
    def site_url(self) -> str:
        return os.getenv("SITE_URL", "") or str(_resolve_env(self.raw.get("site_url", "")) or "")

    @property
    def site_name(self) -> str:
        return os.getenv("SITE_NAME", "") or self.raw.get("site_name", "DeepJobSearchPortal")

    @property
    def retry(self) -> dict[str, Any]:
        return self.raw.get("retry", {})

    @property
    def rate_limit(self) -> dict[str, Any]:
        return self.raw.get("rate_limit", {})

    @property
    def discovery(self) -> dict[str, Any]:
        return self.raw.get("discovery", {})

    @property
    def budget(self) -> dict[str, Any]:
        return self.raw.get("budget", {})

    @property
    def catalog(self):
        """Lazily-built dynamic model catalog (see jobhunter.llm.catalog)."""
        if self._catalog is None:
            from .llm.catalog import ModelCatalog
            object.__setattr__(self, "_catalog", ModelCatalog(self.discovery, MODEL_CATALOG_CACHE))
        return self._catalog

    def chain_for(self, task: str, refresh: bool = False) -> list[str]:
        """Ordered, dynamically-discovered model chain for a task."""
        try:
            return self.catalog.chain(task, refresh=refresh)
        except Exception:  # discovery must never hard-fail the pipeline
            return []

    def params_for(self, task: str) -> dict[str, Any]:
        p = self.raw.get("task_params", {}).get(task, {})
        return {"max_tokens": p.get("max_tokens", 2000), "temperature": p.get("temperature", 0.2)}


@lru_cache(maxsize=4)
def load_profile(path: str | None = None) -> Profile:
    if path:
        p = Path(path)
    else:
        env_profile = os.getenv("JOBHUNTER_PROFILE")
        p = Path(env_profile) if env_profile else CONFIG_DIR / "profile.yaml"
    with open(p, "r", encoding="utf-8") as fh:
        return Profile(yaml.safe_load(fh))


@lru_cache(maxsize=4)
def load_search_matrix(path: str | None = None) -> dict[str, Any]:
    if path:
        p = Path(path)
    else:
        env_matrix = os.getenv("JOBHUNTER_SEARCH_MATRIX")
        p = Path(env_matrix) if env_matrix else CONFIG_DIR / "search_matrix.yaml"
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=4)
def load_model_config(path: str | None = None) -> ModelConfig:
    p = Path(path) if path else CONFIG_DIR / "models.json"
    with open(p, "r", encoding="utf-8") as fh:
        return ModelConfig(json.load(fh))


def ensure_dirs() -> None:
    d = data_dir()
    o = output_dir()
    d.mkdir(parents=True, exist_ok=True)
    o.mkdir(parents=True, exist_ok=True)
