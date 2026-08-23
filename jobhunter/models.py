"""Normalized domain objects shared by every source connector."""
from __future__ import annotations

import hashlib
import html as _html
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_ENTITY = {
    "&amp;": "&", "&nbsp;": " ", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&rsquo;": "'", "&ldquo;": '"', "&rdquo;": '"', "&mdash;": "-",
    "&ndash;": "-", "&hellip;": "...",
}

HIDDEN_SOURCES = {"greenhouse", "lever", "ashby", "workday",
                  "smartrecruiters", "workable", "recruitee", "ycombinator"}


def clean_html(text: str | None) -> str:
    if not text:
        return ""
    out = text.replace("</li>", "\n").replace("<br>", "\n").replace("<br/>", "\n")
    out = out.replace("</p>", "\n\n").replace("<li>", "- ")
    out = _TAG.sub(" ", out)
    out = _html.unescape(out)
    out = re.sub(r"&#\d+;", " ", out)
    lines = [_WS.sub(" ", ln).strip() for ln in out.splitlines()]
    return "\n".join(ln for ln in lines if ln)


_HYBRID_RE = re.compile(
    r"\bhybrid\b|\bflexib\w* (?:work|location)\b|\d\s*days? (?:a week )?in (?:the )?office"
)
# A generic "remote" mention inside a big JD blob (benefits boilerplate, EEO
# text, "some teams offer remote flexibility" etc.) is NOT proof this specific
# posting is remote. Only phrases that assert the *arrangement of this job*
# count as a strong remote signal.
_STRONG_REMOTE_RE = re.compile(
    r"\b(fully remote|100% remote|remote-first|remote first|remote only|"
    r"this (?:is|role is|position is|job is) (?:a |an )?(?:fully )?remote|"
    r"remote position|remote role|remote job|work from home|wfh|"
    r"telecommut\w+|distributed team)\b"
)
_BARE_REMOTE_RE = re.compile(r"\bremote\b")
_ONSITE_RE = re.compile(
    r"\bnot remote\b|\bno remote\b|\bon-?site only\b|\bon-?site\b|\bin-?office\b|\bin person\b|"
    r"\bmust (?:work|be) (?:on-?site|in(?:-| )office)\b"
)
_UNRESTRICTED_LOC_RE = re.compile(r"\bworldwide\b|\banywhere\b")
# For long JD prose, "hybrid" must anchor to this job's own arrangement, not a
# generic mention of company culture/other-teams flexibility.
_STRONG_HYBRID_RE = re.compile(
    r"\d\s*days? (?:a week )?in (?:the )?office|"
    r"this (?:is|role is|position is|job is) (?:a |an )?hybrid|"
    r"hybrid (?:position|role|schedule|work model|working model)"
)


def detect_work_mode(location: str | None = None, title: str | None = None,
                      description: str | None = None) -> str:
    """Return 'remote' | 'hybrid' | 'onsite' | 'unknown'.

    Location field is the most authoritative signal boards give us (it is
    short, structured, and rarely contains unrelated boilerplate). A named
    place ("Bengaluru, India", "Austin, TX") with no remote/hybrid qualifier
    means the role is anchored to that office; a generic "remote" mention
    later in a long JD (benefits text, other-teams policy, etc.) should not
    override that unless it explicitly asserts *this job* is remote.
    """
    loc = (location or "").lower().strip()
    ti = (title or "").lower()
    desc = (description or "").lower()

    if not loc and not ti and not desc:
        return "unknown"

    # 1. Location field verdict (strongest, most specific signal).
    if loc:
        if _HYBRID_RE.search(loc):
            return "hybrid"
        if _BARE_REMOTE_RE.search(loc):
            if _ONSITE_RE.search(loc):
                return "onsite"
            return "remote"
        has_named_place = bool(re.search(r"[a-z]{3,}", loc)) and not _UNRESTRICTED_LOC_RE.search(loc)
    else:
        has_named_place = False

    # 2. Title is short and purpose-built too; trust it almost as much.
    if _HYBRID_RE.search(ti):
        return "hybrid"
    if _BARE_REMOTE_RE.search(ti) and not has_named_place:
        return "remote"

    # 3. Description: only strong, job-specific phrasing counts. A bare
    # "remote" occurrence anywhere in a long JD is treated as unclear on its
    # own once the location already names a real office.
    strong_hybrid = _STRONG_HYBRID_RE.search(desc)
    strong_remote = _STRONG_REMOTE_RE.search(desc)
    strong_onsite = _ONSITE_RE.search(desc)

    if has_named_place:
        # Location already anchors this role to a real office; only an
        # explicit "this job is remote/hybrid" claim can override that.
        if strong_hybrid:
            return "hybrid"
        if strong_remote:
            return "remote"
        return "onsite"

    if strong_hybrid:
        return "hybrid"
    if strong_remote:
        return "remote"
    if _BARE_REMOTE_RE.search(desc):
        return "remote"
    if strong_onsite:
        return "onsite"
    return "unknown"


@dataclass
class Job:
    source: str
    title: str
    company: str
    url: str
    description: str = ""
    location: str = ""
    work_mode: str = "unknown"
    salary: str = ""
    posted_at: str = ""
    department: str = ""
    employment_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # ---------------------------------------------------------------- derived
    @property
    def job_id(self) -> str:
        key = f"{self.company.strip().lower()}|{_norm_title(self.title)}|{self.url.split('?')[0].lower()}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def dedupe_key(self) -> str:
        """Cross-source identity: same role at same company, ignoring the board it came from."""
        return hashlib.sha1(
            f"{self.company.strip().lower()}|{_norm_title(self.title)}".encode("utf-8")
        ).hexdigest()[:16]

    @property
    def is_hidden(self) -> bool:
        """Sourced directly from a company-owned ATS rather than an aggregator."""
        return self.source in HIDDEN_SOURCES

    @property
    def searchable_text(self) -> str:
        return f"{self.title}\n{self.company}\n{self.department}\n{self.location}\n{self.description}"

    def normalize(self) -> "Job":
        self.title = _WS.sub(" ", _html.unescape(self.title or "")).strip()
        self.company = _WS.sub(" ", _html.unescape(self.company or "")).strip()
        self.location = _WS.sub(" ", _html.unescape(self.location or "")).strip()
        self.department = _html.unescape(self.department or "").strip()
        self.description = clean_html(self.description)
        if self.work_mode in ("", "unknown"):
            self.work_mode = detect_work_mode(self.location, self.title, self.description[:4000])
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        d["job_id"] = self.job_id
        d["dedupe_key"] = self.dedupe_key
        d["is_hidden"] = self.is_hidden
        return d


def _norm_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\((?:remote|hybrid|india|contract|full[- ]time)\)", " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\b(senior|sr|junior|jr|lead|staff|principal|i{1,3}|iv|v|1|2|3)\b", " ", t)
    return _WS.sub(" ", t).strip()


@dataclass
class ScoreBreakdown:
    skill_match: float = 0.0          # 0-45
    seniority_alignment: float = 0.0  # 0-20
    remote_flexibility: float = 0.0   # 0-15
    domain_relevance: float = 0.0     # 0-12
    company_growth: float = 0.0       # 0-8
    total: float = 0.0                # 0-100

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass
class JobScore:
    job_id: str
    match_score: float
    breakdown: ScoreBreakdown
    matched_competencies: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    ats_keywords: list[str] = field(default_factory=list)
    seniority_detected: str = "unknown"
    growth_stage: str = "unknown"
    title_family: str = "unknown"   # core | adjacent | unrelated | off-target
    tier: str = "C"
    llm_score: float | None = None
    llm_rationale: str = ""
    llm_model: str = ""
    scored_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["breakdown"] = self.breakdown.to_dict()
        return d


def tier_for(score: float) -> str:
    if score >= 82:
        return "A+"
    if score >= 70:
        return "A"
    if score >= 58:
        return "B"
    return "C"
