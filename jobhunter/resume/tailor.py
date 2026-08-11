"""Tailored, ATS-optimized resume synthesis.

`build_resume_payload()` tries the LLM chain (resume task -> DeepSeek-R1 etc.) and
falls back to `deterministic_tailor()`, a pure-Python relevance-ranking tailor that
needs no API key. Either way the output is validated against the ground-truth
profile so no hallucinated employer, title or date can reach the document.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Profile
from ..llm import LLMError, OpenRouterClient
from ..llm import prompts as P
from ..models import Job

log = logging.getLogger("jobhunter.resume")


@dataclass
class TailoredResume:
    target_title: str
    professional_summary: str
    core_competencies: list[str]
    experience: list[dict[str, Any]]
    technical_skills: dict[str, list[str]]
    certifications: list[str]
    education: list[dict[str, Any]]
    selected_achievements: list[str] = field(default_factory=list)
    ats_keywords_included: list[str] = field(default_factory=list)
    keywords_intentionally_omitted: list[str] = field(default_factory=list)
    ats_alignment_score: float = 0.0
    tailoring_notes: list[str] = field(default_factory=list)
    generated_by: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_title": self.target_title,
            "professional_summary": self.professional_summary,
            "core_competencies": self.core_competencies,
            "experience": self.experience,
            "technical_skills": self.technical_skills,
            "certifications": self.certifications,
            "education": self.education,
            "selected_achievements": self.selected_achievements,
            "ats_keywords_included": self.ats_keywords_included,
            "keywords_intentionally_omitted": self.keywords_intentionally_omitted,
            "ats_alignment_score": self.ats_alignment_score,
            "tailoring_notes": self.tailoring_notes,
            "generated_by": self.generated_by,
        }


# ---------------------------------------------------------------- LLM PATH
def build_resume_payload(job: Job, profile: Profile, score: Any,
                         client: OpenRouterClient | None = None,
                         parsed: dict[str, Any] | None = None) -> TailoredResume:
    ats_keywords = list(getattr(score, "ats_keywords", []) or [])
    angle = getattr(score, "llm_rationale", "") or ""

    if client is not None and client.enabled:
        user = P.RESUME_USER.format(
            profile=profile.as_llm_context(),
            title=job.title, company=job.company,
            location=job.location or "n/a", work_mode=job.work_mode,
            parsed=json.dumps(parsed or {}, indent=2)[:3000] or "(unavailable)",
            match=json.dumps({
                "match_score": getattr(score, "match_score", 0),
                "matched": getattr(score, "matched_competencies", []),
                "gaps": getattr(score, "missing_keywords", []),
            }, indent=2),
            ats_keywords=", ".join(ats_keywords[:24]),
            angle=angle or "Position as an operations/procurement transformation specialist.",
            description=job.description[:7000] or "(none)",
        )
        try:
            data, model = client.json_call("resume", P.RESUME_SYSTEM, user,
                                          schema_hint=P.RESUME_SCHEMA)
            if isinstance(data, dict):
                resume = _from_llm(data, profile, model, job=job, score=score)
                if resume:
                    return resume
        except LLMError as exc:
            log.info("LLM resume generation failed for %s: %s", job.job_id, exc)

    return deterministic_tailor(job, profile, ats_keywords)


def _from_llm(data: dict[str, Any], profile: Profile, model: str,
              *, job: Job, score: Any) -> TailoredResume | None:
    """Validate LLM output against ground truth; repair rather than reject where possible."""
    truth = {(_key(r["company"]), _key(r["title"])): r for r in profile.experience}
    truth_by_company = {_key(r["company"]): r for r in profile.experience}
    allowed_numbers = _ground_truth_numbers(profile)

    experience: list[dict[str, Any]] = []
    for role in data.get("experience") or []:
        if not isinstance(role, dict):
            continue
        company = str(role.get("company", "")).strip()
        title = str(role.get("title", "")).strip()
        ref = truth.get((_key(company), _key(title))) or truth_by_company.get(_key(company))
        if not ref:
            log.warning("dropping hallucinated employer from resume: %r", company)
            continue

        ref_bullets = ref.get("bullets", [])
        bullets = [_clean_bullet(b) for b in (role.get("bullets") or []) if str(b).strip()]
        bullets = _enforce_numeric_truth(bullets, allowed_numbers, ref["company"])
        # Structural anti-fabrication guard: a role cannot grow more bullets than the
        # ground truth supports (rephrasing is fine, inventing new duties is not).
        cap = max(2, min(6, len(ref_bullets)))
        bullets = bullets[:cap]
        if not bullets:
            bullets = [b["text"] for b in ref_bullets[:4]]

        experience.append({
            "company": ref["company"],           # force ground-truth strings
            "title": ref["title"],
            "dates": ref["dates"],
            "location": ref.get("location", ""),
            "bullets": bullets,
        })
    if not experience:
        return None

    # keep chronological ground-truth ordering
    order = {_key(r["company"]): i for i, r in enumerate(profile.experience)}
    experience.sort(key=lambda r: order.get(_key(r["company"]), 99))

    certs = _strlist(data.get("certifications")) or profile.certifications
    valid_certs = {_key(c) for c in profile.certifications}
    certs = [c for c in certs if _key(c) in valid_certs] or profile.certifications

    skills = _ground_truth_skills(data.get("technical_skills"), profile)

    claimable = _claimable_keywords(profile, list(getattr(score, "ats_keywords", []) or []))
    # Preserve an LLM's factual bullet selection but keep the summary itself
    # transferable, so a current employer's domain never dominates the framing.
    summary = _transferable_summary(profile, job, claimable, profile.summary)
    summary = (_enforce_numeric_truth([summary], allowed_numbers, "summary")
               or [re.sub(r"\s+", " ", profile.summary).strip()])[0]

    return TailoredResume(
        # The headline is deterministic so an LLM cannot turn a transferable
        # candidate into an unproven target profession.
        target_title=_safe_target_title(profile, job),
        professional_summary=summary,
        core_competencies=_ground_truth_competencies(data.get("core_competencies"), profile),
        experience=experience,
        technical_skills={str(k): _strlist(v) for k, v in skills.items() if _strlist(v)},
        certifications=certs[:10],
        education=_profile_education(profile),
        selected_achievements=_rank_achievements(profile, job)[:3],
        ats_keywords_included=_strlist(data.get("ats_keywords_included"))[:30],
        keywords_intentionally_omitted=_strlist(data.get("keywords_intentionally_omitted"))[:12],
        ats_alignment_score=_float(data.get("ats_alignment_score")),
        tailoring_notes=_strlist(data.get("tailoring_notes"))[:5],
        generated_by=model,
    )


# ------------------------------------------------------- DETERMINISTIC PATH
def deterministic_tailor(job: Job, profile: Profile, ats_keywords: list[str]) -> TailoredResume:
    """No-API tailoring: rank ground-truth bullets by JD relevance, mirror keywords."""
    jd = f"{job.title} {job.department} {job.description}".lower()
    kw = [k.lower() for k in ats_keywords]

    def relevance(bullet: dict[str, Any]) -> float:
        text = bullet["text"].lower()
        score = 0.0
        for tag in bullet.get("tags", []):
            comp = next((c for c in profile.competencies if c.name == tag), None)
            if comp and any(form in jd for form in comp.surface_forms):
                score += comp.weight
        score += 2.0 * sum(1 for k in kw if k in text)
        score += 0.5 * sum(1 for w in re.findall(r"[a-z]{5,}", text) if w in jd)
        if re.search(r"[₹$]|\d+%|\bRs\.", bullet["text"]):
            score += 3.0   # prefer quantified bullets
        return score

    experience: list[dict[str, Any]] = []
    for role in profile.experience:
        bullets = sorted(role.get("bullets", []), key=relevance, reverse=True)
        n = 5 if role is profile.experience[0] else 5
        keep = [b["text"] for b in bullets[:n]]
        if not keep:
            continue
        experience.append({
            "company": role["company"], "title": role["title"], "dates": role["dates"],
            "location": role.get("location", ""), "bullets": keep,
        })

    # Only mirror keywords the candidate can TRUTHFULLY claim: a JD term is usable
    # if it maps onto a competency alias or a listed skill. This filters out noise
    # like company names and generic filler that the frequency extractor surfaces.
    claimable = _claimable_keywords(profile, ats_keywords)

    summary = re.sub(r"\s+", " ", profile.summary).strip()
    # Multi-word JD phrases carry far more ATS signal than single tokens.
    phrases = [k for k in claimable if " " in k] + [k for k in claimable if " " not in k]
    if phrases:
        focus = ", ".join(phrases[:3])
        summary = f"{summary} Directly aligned to this role through {focus}."

    comps = _default_competencies(profile)
    extra = [k.title() for k in claimable if not any(k in c.lower() for c in comps)]
    comps = (comps + extra)[:12]

    resume_text = (" ".join(b for r in experience for b in r["bullets"]) + " " + summary
                   + " " + " ".join(comps) + " " + " ".join(profile.flat_skills())).lower()
    included = [k for k in ats_keywords if k.lower() in resume_text]
    omitted = [k for k in ats_keywords[:20] if k not in included]
    ats_pct = round(100 * len(included) / max(1, len(ats_keywords[:20])), 1)

    return TailoredResume(
        target_title=_safe_target_title(profile, job),
        professional_summary=_transferable_summary(profile, job, claimable, summary),
        core_competencies=comps,
        experience=experience,
        technical_skills=profile.skills,
        certifications=profile.certifications[:10],
        education=_profile_education(profile),
        selected_achievements=_rank_achievements(profile, job)[:3],
        ats_keywords_included=included[:30],
        keywords_intentionally_omitted=omitted[:12],
        ats_alignment_score=ats_pct,
        tailoring_notes=[
            "Generated without LLM (offline deterministic tailor).",
            "Bullets ranked by JD keyword and competency overlap; quantified bullets prioritised.",
        ],
        generated_by="deterministic",
    )


def _safe_target_title(profile: Profile, job: Job) -> str:
    """Choose a JD-aware, proven role family without copying the applied title."""
    labels = {
        "Strategic Sourcing & Procurement": "Strategic Sourcing & Procurement",
        "Spend Analytics & Cost Optimization": "Spend Analytics & Cost Optimization",
        "Continuous Improvement & Lean Six Sigma": "Continuous Improvement & Process Excellence",
        "Process Excellence & Operating Model": "Process Excellence & Operating Model Transformation",
        "Business / Strategy & Operations Analytics": "Business Operations & Analytics",
        "Workflow Automation & Digital Transformation": "Digital Transformation & Workflow Automation",
        "SAP & Enterprise Systems": "Enterprise Systems & Process Improvement",
        "Program & Project Management": "Program Delivery & Operations",
        "Wealth / Portfolio Management & Financial Services": "Portfolio Analytics & Client Operations",
        "Compliance, Risk & Controls": "Risk, Controls & Operations",
        "Supply Chain & Manufacturing Operations": "Supply Chain & Operations",
        "Leadership & Stakeholder Communication": "Cross-Functional Operations & Stakeholder Management",
    }
    target = f"{job.title} {job.department} {job.description}".lower()
    matched = [
        (sum(form in target for form in competency.surface_forms if len(form) > 3),
         competency.weight, labels.get(competency.name, competency.name))
        for competency in profile.competencies
    ]
    matched = [item for item in matched if item[0] > 0]
    matched.sort(key=lambda item: (-item[0], -item[1], item[2]))
    if matched:
        # Keep a single, strongest proven family so the headline is focused and
        # never rendered as dual slash-separated titles.
        return f"{matched[0][2]} Professional"
    return "Business Operations & Transformation Professional"


def _relevant_competencies(profile: Profile, job: Job) -> list[Any]:
    """Candidate-proven capabilities ordered by target-role relevance."""
    target = f"{job.title} {job.department} {job.description}".lower()
    ranked = []
    for competency in profile.competencies:
        overlap = sum(form in target for form in competency.surface_forms if len(form) > 3)
        ranked.append((overlap, competency.weight, competency))
    return [item[2] for item in sorted(ranked, key=lambda item: (-item[0], -item[1], item[2].name))]


def _profile_education(profile: Profile) -> list[dict[str, Any]]:
    """Keep education achievements exactly as captured in the source profile."""
    return [{"degree": e["degree"], "institution": e["institution"],
             "dates": e["dates"], "details": e.get("details", "")}
            for e in profile.education]


def _rank_achievements(profile: Profile, job: Job) -> list[str]:
    """Prioritize verified achievements whose language maps to the target role."""
    target_terms = set(re.findall(r"[a-z]{4,}", f"{job.title} {job.description}".lower()))
    return sorted(profile.achievements,
                  key=lambda achievement: sum(term in target_terms for term in re.findall(
                      r"[a-z]{4,}", achievement.lower())), reverse=True)


def _transferable_summary(profile: Profile, job: Job, claimable: list[str], fallback: str) -> str:
    """Lead with proven transferable capability, not the current employer's domain."""
    capabilities = [c.name for c in _relevant_competencies(profile, job)[:3]]
    if not capabilities:
        return fallback
    summary = (f"{_safe_target_title(profile, job)} with {profile.years_experience}+ years of experience "
               f"delivering measurable outcomes through {', '.join(capabilities)}.")
    if claimable:
        summary += f" Brings demonstrated experience aligned to {', '.join(claimable[:4])}."
    return summary


def _default_competencies(profile: Profile) -> list[str]:
    return [c.name for c in sorted(profile.competencies, key=lambda c: -c.weight)][:10]


def _ground_truth_competencies(value: Any, profile: Profile) -> list[str]:
    """Retain only LLM competency labels backed by the profile."""
    allowed = {_key(item): item for item in (_default_competencies(profile) + profile.flat_skills())}
    chosen = [allowed[_key(item)] for item in _strlist(value) if _key(item) in allowed]
    return (chosen or _default_competencies(profile))[:12]


def _ground_truth_skills(value: Any, profile: Profile) -> dict[str, list[str]]:
    """Do not let an LLM add an unsupported specialization or tool."""
    if not isinstance(value, dict):
        return profile.skills
    allowed = {_key(skill): (group, skill)
               for group, items in profile.skills.items() for skill in items}
    out: dict[str, list[str]] = {}
    for items in value.values():
        for item in _strlist(items):
            verified = allowed.get(_key(item))
            if verified:
                group, skill = verified
                out.setdefault(group, []).append(skill)
    return out or profile.skills


_GENERIC_KW = re.compile(
    r"^(the|and|our|your|this|that|with|from|will|team|teams|role|job|work|working|company|"
    r"candidate|candidates|experience|years|business|technology|technologies|requirements|"
    r"opportunity|opportunities|environment|responsibilities|solutions?|scalable|"
    r"strong|excellent|ability|including|across|global|world|people|help|drive|driving|"
    r"support|new|best|high|highly|great|good|large|small|full|time)\b", re.I)


def _claimable_keywords(profile: Profile, ats_keywords: list[str]) -> list[str]:
    """JD keywords the candidate can truthfully claim (competency- or skill-backed)."""
    vocab: set[str] = set()
    for comp in profile.competencies:
        vocab.update(comp.surface_forms)
    vocab.update(s.lower() for s in profile.flat_skills())
    profile_text = " ".join([profile.summary.lower(),
                             " ".join(b["text"].lower() for b in profile.all_bullets()),
                             " ".join(profile.flat_skills()).lower()])

    out: list[str] = []
    for kw in ats_keywords:
        k = kw.strip().lower()
        if len(k) < 4 or _GENERIC_KW.match(k):
            continue
        backed = k in profile_text or any(k in v or v in k for v in vocab if len(v) > 3)
        if backed and k not in out:
            out.append(k)
    return out


# ------------------------------------------------- NUMERIC TRUTH ENFORCEMENT
# LLMs reliably invent plausible metrics ("cut turnaround time by 30%"). Every
# quantity in a generated resume must therefore trace back to the ground-truth
# profile, or it is surgically removed — and the bullet dropped if that fails.

_QUANTITY = re.compile(
    r"(?<![\w.])(?:rs\.?\s*|inr\s*|usd\s*|\$|₹)?\s*"
    r"(\d{1,4}(?:[.,]\d{1,3})?)\s*"
    r"(%|percent|m\b|mn\b|k\b|bn\b|cr\b|crores?|lakhs?|l\b|x\b|\+)?", re.I)

# Phrases that can be excised without breaking the sentence.
_TRAILING_CLAIM = re.compile(
    r"[,;]?\s*(?:thereby\s+|thus\s+|which\s+)?"
    r"(?:by|to|of|reaching|achieving|delivering|generating|yielding|worth|up to)\s+"
    r"(?:approximately\s+|approx\.?\s+|around\s+|~\s*|over\s+|more than\s+)?"
    r"(?:rs\.?\s*|inr\s*|usd\s*|\$|₹)?\d{1,4}(?:[.,]\d{1,3})?\s*"
    r"(?:%|percent|m|mn|k|bn|cr|crores?|lakhs?|l|x)?"
    r"(?:\s+(?:uplift|improvement|increase|reduction|gain|growth|savings?|faster|"
    r"points?|percentage points?|within \d+ (?:months?|weeks?|years?)))?", re.I)

_UNIT_ALIAS = {"percent": "%", "mn": "m", "crore": "cr", "crores": "cr",
               "lakhs": "lakh", "l": "lakh", "": ""}


def _norm_quantity(value: str, unit: str | None) -> str:
    v = value.replace(",", "").rstrip("0").rstrip(".") if "." in value else value.replace(",", "")
    u = (unit or "").lower().strip()
    u = _UNIT_ALIAS.get(u, u)
    if u == "+":
        u = ""
    return f"{v}{u}"


def _quantities(text: str) -> set[str]:
    out: set[str] = set()
    for m in _QUANTITY.finditer(text or ""):
        out.add(_norm_quantity(m.group(1), m.group(2)))
    return out


def _ground_truth_numbers(profile: Profile) -> set[str]:
    """Every quantity the candidate can legitimately cite."""
    sources = [profile.summary, profile.headline, str(profile.years_experience)]
    sources += [b["text"] for b in profile.all_bullets()]
    sources += profile.achievements + profile.certifications + profile.flat_skills()
    for e in profile.education:
        sources += [str(e.get("dates", "")), str(e.get("details", "")), str(e.get("degree", ""))]
    for r in profile.experience:
        sources.append(str(r.get("dates", "")))
    allowed = set()
    for s in sources:
        allowed |= _quantities(s)
    # Common non-claim numerals that appear structurally (ordinals, versions, dates).
    allowed |= {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "4.0", "14001", "9001",
                "45001", "5s", "360"}
    return allowed


def _enforce_numeric_truth(bullets: list[str], allowed: set[str], context: str) -> list[str]:
    """Strip or drop any statement containing an unverifiable quantity."""
    kept: list[str] = []
    for bullet in bullets:
        text = bullet
        unverified = _quantities(text) - allowed
        if unverified:
            # 1st attempt: excise the trailing quantified claim(s) surgically.
            for _ in range(3):
                stripped = _TRAILING_CLAIM.sub("", text, count=1).strip(" ,;.") + "."
                stripped = re.sub(r"\s{2,}", " ", stripped)
                if stripped == text or not stripped.strip("."):
                    break
                text = stripped
                if not (_quantities(text) - allowed):
                    break
            unverified = _quantities(text) - allowed
        if unverified:
            log.warning("dropping unverifiable metric(s) %s from %s bullet: %.70s",
                        sorted(unverified), context, bullet)
            continue
        if len(text.split()) >= 6:
            kept.append(text)
    return kept


# --------------------------------------------------------------- utilities
def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _clean_bullet(text: Any) -> str:
    t = re.sub(r"\s+", " ", str(text)).strip()
    t = re.sub(r"^[\-\*\u2022\u25cf\u25aa\s]+", "", t)
    # ATS-hostile glyphs out
    t = t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    return t


def _strlist(v: Any) -> list[str]:
    if isinstance(v, list):
        return [_clean_bullet(x) for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [_clean_bullet(v)]
    return []


def _float(v: Any) -> float:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return 0.0
