"""Relevance & Match Scoring Engine (0-100).

Two layers:
  1. `deterministic_score()` — fast, free, explainable keyword/competency engine.
     Runs on every job. Weighted 45/20/15/12/8 across the five required metrics.
  2. `llm_refine()` — deep-reasoning pass (DeepSeek-R1 chain) on the shortlist only.
     Final score = 0.45 * deterministic + 0.55 * LLM when the LLM pass succeeds.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from .config import Profile
from .llm import LLMError, OpenRouterClient
from .llm import prompts as P
from .models import Job, JobScore, ScoreBreakdown, tier_for

log = logging.getLogger("jobhunter.scoring")

WEIGHTS = {"skill": 45.0, "seniority": 20.0, "remote": 15.0, "domain": 12.0, "growth": 8.0}

SENIORITY_PATTERNS: list[tuple[str, str]] = [
    ("intern", r"\b(intern|internship|trainee|apprentice|graduate program|fresher)\b"),
    ("executive", r"\b(chief|c-?level|cxo|ceo|cfo|coo|cto|president|partner)\b"),
    ("director", r"\b(director|vice president|\bvp\b|svp|evp|head of|general manager)\b"),
    ("senior manager", r"\b(senior manager|sr\.? manager|group manager|associate director)\b"),
    ("manager", r"\b(manager|lead|team lead|principal|assistant manager|deputy manager)\b"),
    ("senior", r"\b(senior|sr\.?|specialist ii|iii\b)\b"),
    ("analyst", r"\b(analyst|associate|executive|coordinator|specialist|consultant)\b"),
]

# distance from the candidate's sweet spot -> score fraction of the 20-pt band
SENIORITY_FIT = {
    "analyst": 0.85, "senior": 1.0, "manager": 1.0, "senior manager": 0.72,
    "director": 0.30, "executive": 0.05, "intern": 0.05, "unknown": 0.60,
}

# --- Title family classification -------------------------------------------
# The job title is by far the strongest fit signal, so it is scored separately
# from body-keyword coverage. Without this the scorer saturates: almost any long
# corporate JD mentions "process", "operations" and "stakeholders" somewhere.
TITLE_CORE = re.compile(
    r"\b(procurement|sourcing|purchas\w+|category manager|spend|supplier|vendor|contract manager|"
    r"continuous improvement|operational excellence|process excellence|process improvement|"
    r"business process|lean|six sigma|kaizen|transformation|"
    r"business operations|biz ops|bizops|business analyst|operations analyst|"
    r"strategy (?:and|&) operations|strategic operations|operations manager|operations lead|"
    r"supply chain|s&op|planning manager|"
    r"management consultant|operations consultant|process consultant|"
    r"program manager|programme manager|pmo|project manager)\b", re.I)

TITLE_ADJACENT = re.compile(
    r"\b(operations|ops|process|excellence|analyst|consultant|strategy|strategic|"
    r"logistics|inventory|warehouse|quality|manufacturing|production|plant|"
    r"revenue operations|revops|people operations|sales operations|marketing operations|"
    r"portfolio|wealth|banking|relationship manager|risk|compliance|audit|"
    r"transformation|change|governance|performance|productivity|efficiency|"
    r"business partner|commercial|finance|fp&a|cost)\b", re.I)

# Titles from entirely different professions — a resume tailored from this profile
# would never be competitive, so they are hard-capped.
TITLE_DISQUALIFY = re.compile(
    r"\b(software|developer|programmer|coding|coder|shopify|"
    r"frontend|front-end|backend|back-end|full[- ]stack|"
    r"devops|sre|site reliability|platform engineer|security engineer|network engineer|"
    r"cloud engineer|cloud devops|cloud platform|cloud infrastructure|"
    r"mechanical engineer|electrical engineer|civil engineer|chemical engineer|"
    r"engineering manager|solutions architect|software architect|data engineer|data scientist|"
    r"machine learning|ml engineer|ai engineer|applied ai|qa engineer|test engineer|software test|automation test|test development|tester|testing|"
    r"compiler|firmware|asic verification|chip engineering|pcb library|deep learning|"
    r"service desk|systems engineer|support engineer|salesforce developer|salesforce engineer|"
    r"servicenow developer|power platform developer|database management|ms sql|middleware|"
    r"react|angular|next\.js|golang|\.net|java backend|python/aws|"
    r"ux|ui designer|graphic designer|copywriter|content writer|journalist|editor|"
    r"account executive|sales representative|sales development|sdr|bdr|"
    r"recruiter|talent acquisition|nurse|physician|therapist|teacher|tutor|professor|"
    r"driver|technician|electrician|welder|chef|barista|cashier|security guard|"
    r"attorney|lawyer|paralegal|social media|influencer|dentist|pharmacist|veterinar\w+)\b", re.I)


def classify_title(title: str, profile: Profile | None = None) -> tuple[str, float]:
    """Return (family, title_fit_points out of 18)."""
    t = title or ""

    is_legal_or_cs = False
    if profile:
        profile_text = f"{profile.name} {profile.headline} {' '.join(c.name for c in profile.competencies)}".lower()
        if any(k in profile_text for k in ["secretar", "company secretary", "governance", "compliance", "law", "ll.b"]):
            is_legal_or_cs = True

    if is_legal_or_cs:
        # For Legal / CS candidate, disqualify ALL software, developer, engineering, and tech roles
        cs_disqualify = re.compile(
            r"\b(software|developer|programmer|coding|coder|shopify|engineer|engineering|architect|"
            r"frontend|front-end|backend|back-end|full[- ]stack|"
            r"devops|sre|site reliability|cloud|platform engineer|security engineer|network|"
            r"data engineer|data scientist|machine learning|ml engineer|ai engineer|tester|testing|"
            r"database|middleware|application|sql|compiler|firmware|"
            r"ux|ui designer|graphic designer|copywriter|content writer|editor|"
            r"sales representative|sdr|bdr|recruiter|talent acquisition|"
            r"nurse|physician|driver|technician|electrician|social media)\b", re.I)
        if cs_disqualify.search(t):
            return "off-target", 0.0

        if re.search(r"\b(company secretary|secretarial|corporate governance|statutory compliance|"
                     r"board governance|entity management|compliance officer|sebi|regulatory compliance|"
                     r"secretarial officer|secretarial analyst|governance manager|corporate secretar\w+|"
                     r"legal entity|assistant company secretary|associate company secretary)\b", t, re.I):
            return "core", 18.0
        if re.search(r"\b(compliance|legal|regulatory|paralegal|law|contract\w*|due diligence|"
                     r"corporate affairs|governance|statutory)\b", t, re.I):
            return "adjacent", 10.5

    if TITLE_DISQUALIFY.search(t):
        return "off-target", 0.0

    if TITLE_CORE.search(t):
        return "core", 18.0
    if TITLE_ADJACENT.search(t):
        return "adjacent", 10.5
    return "unrelated", 3.5


GROWTH_PATTERNS: list[tuple[str, str, float]] = [
    ("seed", r"\b(seed[- ]stage|pre-?seed|founding team|series a)\b", 0.70),
    ("growth startup", r"\b(series [bc]\b|high[- ]growth|hypergrowth|fast[- ]growing|scale[- ]?up)\b", 1.00),
    ("late stage", r"\b(series [d-g]\b|unicorn|pre-?ipo)\b", 0.90),
    ("public", r"\b(nyse|nasdaq|publicly traded|fortune 500|listed company)\b", 0.75),
    ("consultancy", r"\b(consulting firm|advisory|big 4|management consult\w+)\b", 0.85),
    ("enterprise", r"\b(global leader|multinational|mnc|enterprise|\b\d{2},?\d{3}\+? employees)\b", 0.70),
]

_WORD = re.compile(r"[a-z0-9+#/&.\- ]+")
STOP = set("""a an the and or of to in for with on at by from as is are be will you your we our their
this that it its role job work team teams company please apply candidate candidates experience
years year strong ability able across including etc via per new all more most other than who what
must have has had should would can may they them who how when where which while about into over""".split())


def _lower(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def detect_seniority(title: str, description: str) -> str:
    t = _lower(title)
    for level, pat in SENIORITY_PATTERNS:
        if re.search(pat, t):
            return level
    d = _lower(description[:2500])
    for level, pat in SENIORITY_PATTERNS:
        if re.search(pat, d):
            return level
    return "unknown"


def detect_growth_stage(text: str) -> tuple[str, float]:
    low = _lower(text[:8000])
    for stage, pat, frac in GROWTH_PATTERNS:
        if re.search(pat, low):
            return stage, frac
    return "unknown", 0.55


def extract_years_required(description: str) -> tuple[int | None, int | None]:
    low = _lower(description)
    m = re.search(r"(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\s*\+?\s*(?:years|yrs)", low)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})\s*\+\s*(?:years|yrs)", low)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?:minimum|at least|min\.?)\s*(?:of\s*)?(\d{1,2})\s*(?:years|yrs)", low)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(\d{1,2})\s*(?:years|yrs)", low)
    if m:
        return int(m.group(1)), None
    return None, None


def extract_ats_keywords(job: Job, limit: int = 22) -> list[str]:
    """Frequency-ranked domain terms from the JD, used to drive resume mirroring."""
    text = _lower(f"{job.title} {job.department} {job.description}")
    tokens = [t for t in re.findall(r"[a-z][a-z0-9+/#\-\.]{2,}", text) if t not in STOP]
    bigrams = [f"{a} {b}" for a, b in zip(tokens, tokens[1:])
               if a not in STOP and b not in STOP and len(a) > 3 and len(b) > 3]
    counts = Counter(bigrams)
    counts.update({t: c for t, c in Counter(tokens).items() if c >= 2 and len(t) > 4})
    banned = re.compile(r"^(https?|www|com|org|www\.|apply|equal|opportunity|employer|benefits?)")
    ranked = [k for k, _ in counts.most_common(200) if not banned.match(k)]
    seen, out = set(), []
    for k in ranked:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
def deterministic_score(job: Job, profile: Profile) -> JobScore:
    text = job.searchable_text
    low = _lower(text)
    prefs = profile.preferences

    # -- 1. SKILL MATCH (45 = 18 title fit + 27 body coverage) ---------------
    comps = profile.competencies
    total_weight = sum(c.weight for c in comps) or 1
    hit_weight = 0.0
    matched: list[str] = []
    for comp in comps:
        hits = sum(1 for form in comp.surface_forms if form in low)
        if hits:
            matched.append(comp.name)
            # saturating credit: 1 hit = 60% of weight, 4+ hits = full weight
            hit_weight += comp.weight * min(1.0, 0.60 + 0.14 * (hits - 1))

    title_family, title_fit = classify_title(job.title, profile)
    if title_family == "off-target":
        return JobScore(
            job_id=job.job_id,
            match_score=0.0,
            breakdown=ScoreBreakdown(skill_match=0.0, seniority_alignment=0.0,
                                     remote_flexibility=0.0, domain_relevance=0.0,
                                     company_growth=0.0, total=0.0),
            matched_competencies=[],
            missing_keywords=[c.name for c in comps if c.weight >= 8],
            ats_keywords=[],
            seniority_detected="off-target",
            growth_stage="off-target",
            tier="F",
            title_family="off-target",
        )

    # A JD exercises only part of a profile, but demanding 75% of the weighted
    # competency mass keeps the body component genuinely discriminating.
    coverage = min(1.0, (hit_weight / total_weight) / 0.75)
    body_fit = 27.0 * coverage
    if title_family == "unrelated":
        body_fit *= 0.65
    skill = min(WEIGHTS["skill"], title_fit + body_fit)

    # -- 2. SENIORITY ALIGNMENT (20) ----------------------------------------
    level = detect_seniority(job.title, job.description)
    frac = SENIORITY_FIT.get(level, 0.55)
    lo, hi = extract_years_required(job.description)
    yoe = profile.years_experience
    if lo is not None:
        if lo > yoe + 4:
            frac *= 0.35            # far too senior
        elif lo > yoe:
            frac *= 0.75            # slight stretch
        elif hi is not None and hi < yoe - 3:
            frac *= 0.65            # over-qualified / junior role
    seniority = WEIGHTS["seniority"] * frac

    # -- 3. REMOTE FLEXIBILITY (15) -----------------------------------------
    mode = job.work_mode
    allowed_modes = set(prefs.get("work_modes", ["remote", "hybrid"]))
    if "onsite" in allowed_modes and mode == "onsite":
        remote = 0.85
    else:
        remote = {"remote": 1.0, "hybrid": 0.85, "unknown": 0.40, "onsite": 0.10}.get(mode, 0.4)
    loc_low = _lower(job.location)
    if any(_lower(l) in loc_low for l in prefs.get("locations", [])):
        remote = min(1.0, remote + 0.12)
    if re.search(r"\b(us|usa|united states|eu|uk|canada|emea)[- ]only\b|must (?:be|reside) (?:in|located)", low):
        if "india" not in low:
            remote *= 0.45
    remote_flex = WEIGHTS["remote"] * remote

    # -- 4. DOMAIN RELEVANCE (12) -------------------------------------------
    # Scored across distinct domain *families* so a JD cannot farm points by
    # repeating synonyms from one family.
    domain_families = {
        "procurement": ["procurement", "sourcing", "purchasing", "supplier", "vendor", "spend"],
        "supply_chain": ["supply chain", "logistics", "inventory", "s&op", "planning", "warehouse"],
        "operations": ["operations", "operating model", "shop floor", "plant", "manufacturing"],
        "improvement": ["continuous improvement", "lean", "six sigma", "kaizen",
                        "operational excellence", "process improvement"],
        "analytics": ["analytics", "power bi", "tableau", "dashboard", "kpi", "sql", "data-driven"],
        "consulting": ["consulting", "advisory", "transformation", "target operating model"],
        "finance_fs": ["banking", "financial services", "wealth", "portfolio", "investment",
                       "aum", "fp&a", "cost accounting"],
        "compliance": ["compliance", "aml", "kyc", "audit", "iso", "governance", "controls"],
    }
    if any(k in f"{profile.name} {profile.headline}".lower() for k in ["secretar", "company secretary", "governance", "compliance"]):
        domain_families.update({
            "secretarial": ["company secretary", "secretarial", "minutes", "board meeting", "shareholders", "agm", "egm"],
            "statutory": ["statutory", "mca", "roc", "annual return", "filings", "companies act", "share allotment"],
            "governance": ["corporate governance", "diligent", "entity management", "directors", "board", "lodr"],
            "regulatory": ["sebi", "rbi", "fema", "fatca", "crs", "kyc", "due diligence"],
        })
    domain_hits = sum(1 for terms in domain_families.values() if any(t in low for t in terms))
    domain = WEIGHTS["domain"] * min(1.0, domain_hits / 4.0)

    # -- 5. COMPANY GROWTH STAGE (8) ----------------------------------------
    stage, stage_frac = detect_growth_stage(text)
    growth = WEIGHTS["growth"] * stage_frac
    if job.is_hidden:
        growth = min(WEIGHTS["growth"], growth + 0.8)   # hidden-market premium

    # -- penalties -----------------------------------------------------------
    title_low = _lower(job.title)
    penalty = 0.0
    if title_family == "off-target":
        penalty += 22.0        # different profession entirely
    elif title_family == "unrelated":
        penalty += 8.0
    for bad in prefs.get("exclude_keywords", []):
        if _lower(bad) in low:
            penalty += 6.0
    for reject in profile.seniority.get("reject_levels", []):
        if re.search(rf"\b{re.escape(_lower(reject))}\b", title_low):
            penalty += 14.0
            break
    if len(job.description) < 200:
        penalty += 4.0   # thin JD -> low confidence
    if not matched:
        penalty += 10.0

    bd = ScoreBreakdown(
        skill_match=round(skill, 2),
        seniority_alignment=round(seniority, 2),
        remote_flexibility=round(remote_flex, 2),
        domain_relevance=round(domain, 2),
        company_growth=round(growth, 2),
    )
    total = max(0.0, min(100.0, skill + seniority + remote_flex + domain + growth - penalty))
    bd.total = round(total, 2)

    ats_kw = extract_ats_keywords(job)
    missing = [c.name for c in comps if c.name not in matched and c.weight >= 8]

    return JobScore(
        job_id=job.job_id,
        match_score=round(total, 2),
        breakdown=bd,
        matched_competencies=matched,
        missing_keywords=missing,
        ats_keywords=ats_kw,
        seniority_detected=level,
        growth_stage=stage,
        tier=tier_for(total),
        title_family=title_family,
    )


# ---------------------------------------------------------------------------
def parse_jd(client: OpenRouterClient, job: Job) -> tuple[dict[str, Any], str]:
    """LLM structured extraction of JD requirements."""
    user = P.PARSE_USER.format(
        title=job.title, company=job.company, location=job.location,
        source=job.source, description=job.description[:9000] or "(no description available)",
    )
    parsed, model = client.json_call("parse", P.PARSE_SYSTEM, user, schema_hint=P.PARSE_SCHEMA)
    return (parsed if isinstance(parsed, dict) else {}), model


def llm_refine(client: OpenRouterClient, job: Job, profile: Profile, base: JobScore,
               parsed: dict[str, Any] | None = None) -> JobScore:
    """Deep-reasoning re-score. Returns a blended JobScore; falls back silently."""
    import json

    try:
        if parsed is None:
            parsed, _ = parse_jd(client, job)
    except LLMError as exc:
        log.info("JD parse failed for %s: %s", job.job_id, exc)
        parsed = {}

    user = P.MATCH_USER.format(
        profile=profile.as_llm_context(),
        title=job.title, company=job.company, location=job.location or "n/a",
        work_mode=job.work_mode, source=job.source,
        hidden_note="(HIDDEN JOB — sourced from company ATS, low applicant competition)"
                    if job.is_hidden else "",
        parsed=json.dumps(parsed, indent=2)[:3000] if parsed else "(unavailable)",
        description=_trim(job.description, 5000) or "(none)",
        prescore=json.dumps(base.breakdown.to_dict()),
    )
    try:
        result, model = client.json_call("match", P.MATCH_SYSTEM, user, schema_hint=P.MATCH_SCHEMA)
    except LLMError as exc:
        log.info("LLM match failed for %s: %s", job.job_id, exc)
        base.llm_rationale = f"LLM unavailable: {exc}"
        return base
    if isinstance(result, dict):
        _apply_llm_result(base, result, model)
    return base


def batch_llm_refine(client: OpenRouterClient, pairs: list[tuple[Job, JobScore]],
                     profile: Profile, batch_size: int = 6,
                     max_calls: int | None = None) -> dict[str, dict[str, Any]]:
    """Score many jobs per request — the primary API-budget optimisation.

    Mutates each JobScore in place with the blended result and returns the raw
    per-job LLM analysis keyed by job_id (reused later for resume tailoring so no
    second 'parse' request is ever needed).
    """
    import json

    analyses: dict[str, dict[str, Any]] = {}
    if not pairs or not client.enabled:
        return analyses

    batches = [pairs[i:i + batch_size] for i in range(0, len(pairs), batch_size)]
    if max_calls is not None:
        batches = batches[:max(0, max_calls)]

    profile_ctx = profile.as_llm_context()

    for batch in batches:
        refs = {f"J{idx + 1}": (job, score) for idx, (job, score) in enumerate(batch)}
        blocks = [
            P.BATCH_JOB_BLOCK.format(
                ref=ref, title=job.title, company=job.company,
                location=job.location or "n/a", work_mode=job.work_mode, source=job.source,
                hidden_note=" | HIDDEN JOB (company ATS, low applicant competition)"
                            if job.is_hidden else "",
                description=_trim(job.description, 2600) or "(no description available)",
            )
            for ref, (job, score) in refs.items()
        ]
        user = P.BATCH_USER.format(profile=profile_ctx, count=len(batch), jobs="\n".join(blocks))

        try:
            data, model = client.json_call("match", P.BATCH_SYSTEM, user,
                                          schema_hint=P.BATCH_SCHEMA,
                                          max_tokens=700 + 380 * len(batch))
        except LLMError as exc:
            log.info("batch match failed (%d jobs): %s", len(batch), exc)
            if "budget" in str(exc).lower():
                break
            continue

        for idx, (ref, res) in enumerate(_normalize_batch(data, list(refs))):
            entry = refs.get(ref) or (list(refs.values())[idx] if idx < len(refs) else None)
            if not entry:
                continue
            job, score = entry
            _apply_llm_result(score, res, model)
            analyses[job.job_id] = res

    return analyses


def _normalize_batch(data: Any, order: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Accept every shape free models actually emit for a batch response.

    Handles: {"results":[...]}, a bare list, {"J1":{...},"J2":{...}}, and results whose
    sub-scores are nested under "sub_scores"/"scores"/"breakdown".
    """
    rows: list[tuple[str, dict[str, Any]]] = []

    if isinstance(data, dict):
        container = None
        for key in ("results", "jobs", "scores", "data", "output"):
            if isinstance(data.get(key), list):
                container = data[key]
                break
        if container is not None:
            for i, res in enumerate(container):
                if isinstance(res, dict):
                    ref = str(res.get("job_ref") or res.get("ref") or
                              (order[i] if i < len(order) else "")).strip()
                    rows.append((ref, _flatten_subscores(res)))
        else:
            # keyed-by-ref object, e.g. {"J1": {...}}
            for ref in order:
                res = data.get(ref)
                if isinstance(res, dict):
                    rows.append((ref, _flatten_subscores(res)))
            if not rows:
                for k, v in data.items():
                    if isinstance(v, dict) and re.fullmatch(r"J?\d+", str(k).strip(), re.I):
                        rows.append((str(k).strip(), _flatten_subscores(v)))
    elif isinstance(data, list):
        for i, res in enumerate(data):
            if isinstance(res, dict):
                ref = str(res.get("job_ref") or res.get("ref") or
                          (order[i] if i < len(order) else "")).strip()
                rows.append((ref, _flatten_subscores(res)))
    return rows


_SUBSCORE_KEYS = ("skill_match", "seniority_alignment", "remote_flexibility",
                  "domain_relevance", "company_growth")


def _flatten_subscores(res: dict[str, Any]) -> dict[str, Any]:
    out = dict(res)
    for nest in ("sub_scores", "subscores", "scores", "breakdown", "components"):
        inner = res.get(nest)
        if isinstance(inner, dict):
            for k in _SUBSCORE_KEYS:
                if k not in out and k in inner:
                    out[k] = inner[k]
    return out


def _apply_llm_result(score: JobScore, result: dict[str, Any], model: str) -> None:
    """Blend one LLM analysis into a deterministic JobScore (45% det / 55% LLM)."""
    llm_total = _num(result.get("match_score"), score.match_score)
    blended = round(0.45 * score.match_score + 0.55 * llm_total, 2)

    score.breakdown = ScoreBreakdown(
        skill_match=round(_blend(score.breakdown.skill_match, result.get("skill_match"), 45), 2),
        seniority_alignment=round(_blend(score.breakdown.seniority_alignment,
                                         result.get("seniority_alignment"), 20), 2),
        remote_flexibility=round(_blend(score.breakdown.remote_flexibility,
                                        result.get("remote_flexibility"), 15), 2),
        domain_relevance=round(_blend(score.breakdown.domain_relevance,
                                      result.get("domain_relevance"), 12), 2),
        company_growth=round(_blend(score.breakdown.company_growth,
                                    result.get("company_growth"), 8), 2),
        total=blended,
    )
    score.match_score = blended
    score.tier = tier_for(blended)
    score.llm_score = llm_total
    score.llm_model = model
    rec = str(result.get("recommendation") or "").strip().upper()
    parts = [rec, str(result.get("rationale") or "").strip(),
             str(result.get("positioning_angle") or "").strip()]
    score.llm_rationale = " | ".join(p for p in parts if p)[:1200]
    if result.get("seniority"):
        score.seniority_detected = str(result["seniority"])
    if result.get("company_growth_stage"):
        score.growth_stage = str(result["company_growth_stage"])
    ats = _strlist(result.get("ats_keywords_to_mirror")) + _strlist(result.get("must_have_skills"))
    if ats:
        score.ats_keywords = list(dict.fromkeys(ats))[:24]
    score.matched_competencies = list(dict.fromkeys(
        score.matched_competencies + _strlist(result.get("matched_strengths"))))[:14]
    gaps = _strlist(result.get("gaps"))
    if gaps:
        score.missing_keywords = gaps[:12]


def _trim(text: str, limit: int) -> str:
    """Token-frugal truncation: keep the head (requirements live near the top)."""
    t = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + " …"


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return default


def _blend(det: float, llm: Any, cap: float) -> float:
    l = _num(llm, det)
    l = min(l, cap)
    return max(0.0, min(cap, 0.45 * det + 0.55 * l))


def _strlist(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []
