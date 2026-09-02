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
        core_competencies=_ground_truth_competencies(data.get("core_competencies"), profile, job),
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

    # Best-match competencies first; weight-ordered fill only so the section
    # never looks empty (5-chip floor).
    comps = _jd_matched_competency_names(profile, job)[:12]
    if len(comps) < 5:
        comps += [c for c in _default_competencies(profile) if c not in comps]
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


_HEADLINE_STOPWORDS = {"a", "an", "and", "at", "for", "in", "of", "the", "to", "with",
                       "associate", "executive", "junior", "lead", "manager", "senior"}
_TITLE_LEVEL_WORDS = {
    "i", "ii", "iii", "iv", "v", "sr", "sr.", "jr", "jr.", "senior", "junior",
    "lead", "staff", "principal", "associate", "entry", "level", "intern",
    "internship", "manager", "director", "head", "vp", "vice", "president",
    "chief", "officer", "1", "2", "3", "4",
}
# Nouns that make a title read as an actual role. If stripping level words
# leaves none of these, the stripped title is a mangled fragment (e.g.
# "Senior Account Manager" -> "Account") and the original wording is kept.
_TITLE_ROLE_NOUNS = {
    "manager", "analyst", "engineer", "specialist", "consultant", "architect",
    "scientist", "coordinator", "supervisor", "lead", "adviser", "advisor",
    "planner", "executive", "partner", "director", "officer",
    "administrator", "associate", "designer", "operator",
}
_CORPORATE_PREFIX_RE = re.compile(
    r"\b(?:GN|S&C|MC|PRH|IE|I&E|SC&E|TGP|CTS|GDS|FY\d{2})\b|"
    r"\b(?:Industry\s*X|Digital\s*Engineering\s*R&D)\b|"
    r"\b(?:Req|ReqID|Job\s*ID|ID|Ref|Position\s*#?)\s*[:#-]?\s*[A-Z0-9_\-]+|"
    r"\b[A-Z]{1,4}\d{5,}\b",
    re.I
)


def _headline_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _HEADLINE_STOPWORDS}


def _clean_job_title_for_headline(job_title: str) -> str:
    """Strip corporate requisition codes, level noise, and taxonomy artifacts from a JD title."""
    raw = job_title or ""
    t = _CORPORATE_PREFIX_RE.sub(" ", raw)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"\((?:[^)]*(?:remote|hybrid|india|full[- ]time|contract|req|id|tier|\d)[^)]*)\)", " ", t, flags=re.I)
    t = re.sub(r"[-–—/|]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -,–—|")
    # Drop corporate domain tokens that leak in from portals ("Amazon.jobs",
    # "Accenture.wd103.myworkdayjobs.com").
    t = re.sub(r"\b[A-Za-z][A-Za-z0-9\-]*(?:\.[A-Za-z]{2,})+\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -,–—|")

    # Normalize abbreviations into standard professional phrases
    t = re.sub(r"\bOps\b", "Operations", t, flags=re.I)
    t = re.sub(r"\bCI\b", "Continuous Improvement", t)
    t = re.sub(r"\bSCM\b", "Supply Chain", t)
    t = re.sub(r"\bProc\b", "Procurement", t, flags=re.I)
    t = re.sub(r"\bProfessional\b", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()

    # Split on slash or pipe dividers to avoid dual roles
    parts = re.split(r"\s*[/|]\s*", t)
    cleaned_parts: list[str] = []
    seen_words: set[str] = set()
    for part in parts:
        words = [w for w in re.split(r"\s+", part.strip()) if w]
        kept = [w for w in words if w.lower().strip(".,") not in _TITLE_LEVEL_WORDS]
        # Stripping level words must not eat the role itself
        # ("Senior Account Manager" -> "Account"): if no role noun survives,
        # keep the original wording.
        if kept and not any(w.lower() in _TITLE_ROLE_NOUNS for w in kept):
            kept = list(words)
        phrase = " ".join(kept).strip(" -,")
        if not phrase:
            continue
        key = phrase.lower()
        if key in seen_words:
            continue
        seen_words.add(key)
        cleaned_parts.append(phrase)
    if not cleaned_parts:
        return t
    deduped: list[str] = []
    for phrase in cleaned_parts:
        phrase_words = _headline_words(phrase)
        if any(phrase_words <= _headline_words(existing) for existing in deduped):
            continue
        deduped.append(phrase)
    return " & ".join(deduped[:2]) if deduped else t


def _safe_target_title(profile: Profile, job: Job) -> str:
    """Synthesize a calibrated target title that bridges candidate grounding with target JD requirements."""
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
    fallback = matched[0][2] if matched else "Business Operations & Transformation"

    raw = job.title or ""
    # Strip corporate codes and normalize
    cleaned = _clean_job_title_for_headline(raw)
    cleaned_words = [w for w in cleaned.split() if len(w) > 2 and w.lower() not in {"and", "for", "the", "with"}]

    # If the cleaned title became trivial (e.g. just "Analyst"), synthesize with the top competency domain
    if len(cleaned_words) <= 1:
        if "analyst" in raw.lower():
            return f"{fallback} Analyst" if "Analyst" not in fallback else fallback
        elif "manager" in raw.lower():
            return f"{fallback} Manager" if "Manager" not in fallback else fallback
        elif "lead" in raw.lower():
            return f"{fallback} Lead" if "Lead" not in fallback else fallback
        return fallback

    # Check candidate grounding
    grounded_text = " ".join([
        profile.headline,
        " ".join(role.get("title", "") for role in profile.experience),
        " ".join(profile.flat_skills()),
        " ".join(c.name for c in profile.competencies),
        " ".join(f for c in profile.competencies for f in c.surface_forms)
    ]).lower()
    grounded_words = set(re.findall(r"[a-z]{3,}", grounded_text))
    t_words = set(re.findall(r"[a-z]{3,}", cleaned.lower()))

    overlap_ratio = len(t_words & grounded_words) / len(t_words) if t_words else 0
    if overlap_ratio >= 0.5:
        return cleaned

    return fallback


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


_HIGH_IMPACT_ACHIEVEMENT_RE = re.compile(
    r"\bpatent(?:s|ed)?\b|\baward(?:s|ed)?\b|\bwinner\b|\bwon\b|\brecognition\b|"
    r"\brecognized\b|\bhonou?r(?:s|ed)?\b|\bpublication\b|\bpublished\b|"
    r"\bgranted\b|\btop performer\b|\branked\s*#?\s*1\b|\bfirst place\b|"
    r"\bgold medal\b|\bcertificat\w*\b",
    re.I,
)
_PATENT_CLAIM_RE = re.compile(r"\bpatents?\b", re.I)
_AWARD_LIKE_RE = re.compile(
    r"\baward\b|\brecognition\b|\bplaque\b|\bhackathon\b|\bwinner\b|\bhonou?r\b|"
    r"\btournament\b|\bcertificat\w*\b",
    re.I,
)


def _synthesized_patent_summary(profile: Profile) -> str | None:
    """If the achievements list contains an unlabelled cluster of individual
    patent write-ups (title: description), summarise them as one verified,
    countable bullet so the count itself (e.g. "6 patents") isn't lost to
    JD-overlap ranking. Only fires when some OTHER achievement already claims
    a patent portfolio in words, keeping the synthesis anchored to a real
    candidate statement rather than a guess.
    """
    if not any(_PATENT_CLAIM_RE.search(a) for a in profile.achievements):
        return None
    patent_items = [
        a for a in profile.achievements
        if ":" in a and not _AWARD_LIKE_RE.search(a) and not _PATENT_CLAIM_RE.search(a)
    ]
    if len(patent_items) < 2:
        return None
    titles = [a.split(":", 1)[0].strip() for a in patent_items]
    lead = ", ".join(titles[:2])
    remaining = len(patent_items) - 2
    tail = f", and {remaining} more." if remaining > 0 else "."
    return (f"Co-invented {len(patent_items)} technical innovations recognised as "
            f"patented work, including {lead}{tail}")


def _rank_achievements(profile: Profile, job: Job) -> list[str]:
    """Prioritize verified achievements whose language maps to the target role.

    Standout categories (patents, awards, publications, top rankings) are
    boosted so they surface even when their wording doesn't overlap much with
    JD keywords — a "6 patents filed" line is a differentiator for almost any
    role, not just ones that literally say "patent".
    """
    target_terms = set(re.findall(r"[a-z]{4,}", f"{job.title} {job.description}".lower()))
    patent_summary = _synthesized_patent_summary(profile)
    achievements = list(profile.achievements)
    if patent_summary:
        raw_items = [
            a for a in profile.achievements
            if ":" in a and not _AWARD_LIKE_RE.search(a) and not _PATENT_CLAIM_RE.search(a)
        ]
        achievements = [a for a in achievements if a not in raw_items]
        achievements.append(patent_summary)

    def rank(achievement: str) -> tuple[int, int]:
        overlap = sum(term in target_terms for term in re.findall(r"[a-z]{4,}", achievement.lower()))
        is_patent_summary = achievement == patent_summary
        high_impact = bool(_HIGH_IMPACT_ACHIEVEMENT_RE.search(achievement)) or is_patent_summary
        bonus = 4 if is_patent_summary else (3 if high_impact else 0)
        return (overlap + bonus, int(bool(_quantities(achievement))))

    return sorted(achievements, key=rank, reverse=True)


def _transferable_summary(profile: Profile, job: Job, claimable: list[str], fallback: str) -> str:
    """Build a focused, target-aligned summary from proven profile evidence."""
    capabilities = [c.name for c in _relevant_competencies(profile, job)[:3]]
    if not capabilities:
        return fallback

    evidence, current_bullet = _summary_evidence(profile, job, capabilities)
    # The candidate's current-role flagship result (e.g. a big-AUM portfolio line)
    # should always headline the summary when it is JD-relevant at all; otherwise
    # older-tenure bullets can crowd it out.
    if current_bullet and current_bullet not in evidence:
        evidence = [current_bullet] + evidence
        evidence = evidence[:2]
    capability_text = ", ".join(_summary_capability_label(c) for c in capabilities)
    summary = (f"{_safe_target_title(profile, job)} professional with "
               f"{profile.years_experience}+ years of experience across {capability_text}.")
    if evidence:
        summary += " " + " ".join(evidence)
    else:
        summary += " " + re.sub(r"\s+", " ", fallback).strip()

    mba = next((e for e in profile.education if "mba" in str(e.get("degree", "")).lower()), None)
    if mba:
        summary += f" MBA, {mba.get('institution', '')}."
    return summary


def _summary_capability_label(capability: str) -> str:
    labels = {
        "Business / Strategy & Operations Analytics": "business and operations analytics",
        "Process Excellence & Operating Model": "process excellence and operating-model improvement",
        "Program & Project Management": "program and project delivery",
        "Continuous Improvement & Lean Six Sigma": "continuous improvement and Lean Six Sigma",
        "Spend Analytics & Cost Optimization": "spend analytics and cost optimization",
        "Strategic Sourcing & Procurement": "strategic sourcing and procurement",
        "Workflow Automation & Digital Transformation": "workflow automation and digital transformation",
        "SAP & Enterprise Systems": "SAP and enterprise systems",
        "Leadership & Stakeholder Communication": "cross-functional stakeholder management",
    }
    return labels.get(capability, capability)


def _summary_evidence(profile: Profile, job: Job, capabilities: list[str]) -> tuple[list[str], str | None]:
    """Select the strongest JD-relevant, source-verbatim achievements for the summary.

    Returns (top_bullets, current_role_bullet) where current_role_bullet is the
    first (in profile order) bullet of the candidate's present role with genuine
    JD overlap — the profile author's flagship for that role — so callers can
    guarantee it a summary slot.
    """
    target = f"{job.title} {job.department} {job.description}".lower()
    selected: list[tuple[float, str]] = []
    capability_set = set(capabilities)
    current_best: tuple[float, str] | None = None
    for role in profile.experience:
        # Internship evidence is retained in experience, but should not displace
        # substantive career outcomes in the executive summary.
        is_internship = "intern" in str(role.get("title", "")).lower()
        is_current = (not role.get("end")) or "present" in str(role.get("dates", "")).lower()
        for bullet in role.get("bullets", []):
            text = str(bullet.get("text", "")).strip()
            if not text:
                continue
            direct_overlap = 0
            relevant_tags = 0
            for tag in bullet.get("tags", []):
                comp = next((c for c in profile.competencies if c.name == tag), None)
                if comp:
                    matches = sum(form in target for form in comp.surface_forms if len(form) > 3)
                    direct_overlap += matches
                    if tag in capability_set and matches:
                        relevant_tags += 1
            score = (direct_overlap * 5.0) + (relevant_tags * 3.0)
            if re.search(r"[₹$]|\d+%|\b(?:\d{2,}|one|two|three|four|five|six|seven|eight|nine)\+?\b", text, re.I):
                score += 2.0
            if is_internship:
                score -= 10.0
            selected.append((score, text))
            # Only treat the current-role bullet as flagship-worthy when it has
            # real JD overlap (the quantified-number bonus alone is not enough).
            # First match in profile order wins: the author's top bullet IS the
            # flagship, even if a later bullet scores higher on JD keywords.
            if (current_best is None and is_current and not is_internship
                    and (direct_overlap or relevant_tags)):
                current_best = (score, text)

    selected.sort(key=lambda item: (-item[0], -len(item[1]), item[1]))
    return [text for score, text in selected if score > 0][:2], (
        current_best[1] if current_best else None
    )


def _default_competencies(profile: Profile) -> list[str]:
    return [c.name for c in sorted(profile.competencies, key=lambda c: -c.weight)][:10]


def _jd_matched_competency_names(profile: Profile, job: Job | None) -> list[str]:
    """Profile competency names that overlap with the target JD, best match first."""
    if job is None:
        return []
    target = f"{job.title} {job.department} {job.description}".lower()
    ranked = []
    for comp in profile.competencies:
        overlap = sum(form in target for form in comp.surface_forms if len(form) > 3)
        if overlap:
            ranked.append((overlap, comp.weight, comp.name))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [name for _, _, name in ranked]


def _ground_truth_competencies(value: Any, profile: Profile, job: Job | None = None) -> list[str]:
    """Retain only LLM competency labels backed by the profile.

    Best-match-first: JD-overlapping competencies lead, and the list is only
    backfilled up to a 5-chip floor (JD matches first, then weight order) so a
    paraphrased LLM response can no longer shrink the section to two chips.
    """
    allowed = {_key(item): item for item in (_default_competencies(profile) + profile.flat_skills())}
    chosen = [allowed[_key(item)] for item in _strlist(value) if _key(item) in allowed]
    if len(chosen) < 5:
        backfill = _jd_matched_competency_names(profile, job)
        backfill += [n for n in _default_competencies(profile) if n not in backfill]
        seen = {_key(item) for item in chosen}
        for name in backfill:
            if len(chosen) >= 5:
                break
            if _key(name) not in seen:
                chosen.append(name)
                seen.add(_key(name))
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
