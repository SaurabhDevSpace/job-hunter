"""Prompt templates for JD parsing, deep match reasoning and ATS resume synthesis."""
from __future__ import annotations

# ---------------------------------------------------------------- JD PARSING
PARSE_SYSTEM = """You are a precision job-description parser for an ATS pipeline.
Extract only what is explicitly stated. Never guess. Use null for unknown scalars
and [] for unknown lists. Output JSON only."""

PARSE_SCHEMA = """{
  "role_family": "string  // e.g. Procurement, Continuous Improvement, Business Operations, Strategy & Ops",
  "seniority": "intern|analyst|associate|specialist|senior|lead|manager|senior manager|director|executive|unknown",
  "years_experience_min": "number|null",
  "years_experience_max": "number|null",
  "work_mode": "remote|hybrid|onsite|unknown",
  "location_constraints": ["string"],
  "must_have_skills": ["string"],
  "nice_to_have_skills": ["string"],
  "hard_ats_keywords": ["string  // 12-20 exact terms an ATS would screen on"],
  "tools_and_systems": ["string"],
  "key_responsibilities": ["string  // max 6, condensed"],
  "success_metrics": ["string  // KPIs / outcomes the role owns"],
  "company_growth_stage": "seed|early startup|growth startup|scaleup|late stage|public|enterprise|consultancy|unknown",
  "industry": "string|null",
  "compensation": "string|null",
  "red_flags": ["string  // e.g. unpaid, citizenship requirement, relocation mandatory"]
}"""

PARSE_USER = """JOB TITLE: {title}
COMPANY: {company}
LOCATION: {location}
SOURCE: {source}

--- JOB DESCRIPTION ---
{description}
--- END ---

Parse into the required JSON schema."""


# ------------------------------------------------------------ DEEP MATCHING
MATCH_SYSTEM = """You are an exacting executive career strategist and ATS analyst.
You score candidate-to-role fit conservatively and defensibly. A high score requires
evidence in the candidate profile — transferable potential is NOT the same as proven fit.
Penalise seniority mismatch, hard location/citizenship constraints and missing must-have
domain skills. Output JSON only."""

MATCH_SCHEMA = """{
  "match_score": "number 0-100 (overall fit)",
  "skill_match": "number 0-45",
  "seniority_alignment": "number 0-20",
  "remote_flexibility": "number 0-15",
  "domain_relevance": "number 0-12",
  "company_growth": "number 0-8",
  "matched_strengths": ["string  // candidate evidence that directly satisfies a JD requirement"],
  "gaps": ["string  // JD requirements with no evidence in the profile"],
  "ats_keywords_to_mirror": ["string  // 10-18 exact JD terms the resume must contain"],
  "positioning_angle": "string  // 1-2 sentences: how to frame this candidate for THIS role",
  "recommendation": "apply now|apply with tailoring|stretch|skip",
  "rationale": "string  // <=80 words, cite specifics"
}"""

MATCH_USER = """=== CANDIDATE GROUND TRUTH ===
{profile}

=== TARGET ROLE ===
Title: {title}
Company: {company}
Location: {location} | Work mode: {work_mode}
Source: {source} {hidden_note}

Parsed requirements:
{parsed}

Raw description (truncated):
{description}

=== DETERMINISTIC PRE-SCORE (keyword engine, for calibration only) ===
{prescore}

Score the fit. Sub-scores must sum to match_score (+/-2)."""


# ------------------------------------------------- BATCHED TRIAGE (BUDGET)
# One request scores several jobs at once. On a free OpenRouter key this is the
# single biggest saving: 12 jobs cost 2 requests instead of 24.
BATCH_SYSTEM = """You are an exacting executive career strategist and ATS analyst.
You will receive ONE candidate profile and SEVERAL job postings. Score each posting
independently and conservatively: proven evidence in the profile only — transferable
potential is NOT proven fit. Penalise seniority mismatch, hard location/citizenship
constraints and missing must-have domain skills.

MANDATORY:
- Return a top-level object with a "results" array holding ONE object per job.
- Echo job_ref exactly. Populate EVERY field, including the text fields.
- Derive every number yourself from the profile-to-posting evidence. Numbers must
  differ between jobs unless the fit is genuinely identical.
- ats_keywords_to_mirror must be exact terms lifted from that job's description.
Output JSON only, no prose, no markdown fences."""

BATCH_SCHEMA = """{
  "results": [
    {
      "job_ref": "string  // echo the JOB_REF exactly",
      "match_score": "number 0-100",
      "skill_match": "number 0-45",
      "seniority_alignment": "number 0-20",
      "remote_flexibility": "number 0-15",
      "domain_relevance": "number 0-12",
      "company_growth": "number 0-8",
      "seniority": "analyst|senior|manager|senior manager|director|executive|intern|unknown",
      "company_growth_stage": "seed|growth startup|scaleup|late stage|public|enterprise|consultancy|unknown",
      "matched_strengths": ["string  // max 5"],
      "gaps": ["string  // max 5"],
      "ats_keywords_to_mirror": ["string  // 10-16 exact JD terms"],
      "must_have_skills": ["string  // max 8"],
      "positioning_angle": "string  // 1 sentence",
      "recommendation": "apply now|apply with tailoring|stretch|skip",
      "rationale": "string  // <=45 words"
    }
  ]
}"""

BATCH_USER = """=== CANDIDATE GROUND TRUTH ===
{profile}

=== {count} JOB POSTINGS TO SCORE ===
{jobs}

Score all {count} postings. Sub-score caps: skill_match 45, seniority_alignment 20,
remote_flexibility 15, domain_relevance 12, company_growth 8. The five sub-scores must
sum to match_score (+/-2). Fill in matched_strengths, gaps, ats_keywords_to_mirror,
must_have_skills, positioning_angle, recommendation and rationale for every job."""

BATCH_JOB_BLOCK = """--- JOB_REF: {ref} ---
Title: {title}
Company: {company}
Location: {location} | Work mode: {work_mode} | Source: {source}{hidden_note}
Description (truncated):
{description}
"""


# --------------------------------------------------------- RESUME SYNTHESIS
RESUME_SYSTEM = """You are an elite executive resume writer specialising in ATS-optimised,
single-page-dense resumes for operations, procurement and transformation roles.

ABSOLUTE RULES
1. TRUTH: use only facts present in the candidate ground truth. Never invent employers,
   titles, dates, tools, certifications or metrics. You MAY rephrase, re-order, merge and
   re-emphasise existing facts, and you MAY surface an existing metric in a different unit.
2. ATS: mirror the exact keyword strings supplied in ats_keywords wherever truthful.
   No tables, columns, graphics, icons or special glyphs. Plain text bullets only.
3. IMPACT: every bullet = strong past-tense action verb + what was done + quantified result.
   Target 18-30 words per bullet. Never start two bullets in the same role with the same verb.
4. RELEVANCE: order bullets by relevance to THIS job description, most relevant first.
5. Keep the professional summary to 3 sentences, written in third person with no pronouns.
Output JSON only."""

RESUME_SCHEMA = """{
  "target_title": "string  // credible candidate headline: similar to the target role but not a copy of the JD title; grounded in the candidate's existing profession",
  "professional_summary": "string  // 3 sentences, no pronouns, JD-mirrored keywords",
  "core_competencies": ["string  // 9-12 short keyword phrases, ATS-mirrored"],
  "experience": [
    {
      "company": "string (must match ground truth exactly)",
      "title": "string (must match ground truth exactly)",
      "dates": "string (must match ground truth exactly)",
      "location": "string",
      "bullets": ["string  // 3-6 rewritten, JD-targeted bullets"]
    }
  ],
  "technical_skills": {"Category name": ["skill"]},
  "certifications": ["string"],
  "education": [{"degree": "string", "institution": "string", "dates": "string", "details": "string"}],
  "selected_achievements": ["string  // max 3"],
  "ats_keywords_included": ["string"],
  "keywords_intentionally_omitted": ["string  // JD keywords the candidate cannot truthfully claim"],
  "ats_alignment_score": "number 0-100  // your estimate of this resume's ATS pass rate for this JD",
  "tailoring_notes": ["string  // max 4 notes on what was emphasised and why"]
}"""

RESUME_USER = """=== CANDIDATE GROUND TRUTH (the ONLY permitted source of facts) ===
{profile}

=== TARGET JOB ===
Title: {title}
Company: {company}
Location: {location} | Work mode: {work_mode}

Parsed requirements:
{parsed}

Match analysis:
{match}

MUST-MIRROR ATS KEYWORDS: {ats_keywords}
POSITIONING ANGLE: {angle}

Raw job description (truncated):
{description}

Produce the tailored, ATS-optimised resume JSON now. The target_title must NOT be identical
to the target job title. Create a closely related, truthful headline using the candidate's
original professional identity and transferable strengths; never claim the candidate held
the target employer's exact role title."""


# ------------------------------------------------------------- COVER LETTER
COVER_SYSTEM = """You write concise, senior-level cover letters. 4 paragraphs, max 260 words
total, no clichés ("I am writing to apply", "team player", "hit the ground running"), no
invented facts, and one concrete quantified proof point per body paragraph. Output JSON only."""

COVER_SCHEMA = """{
  "greeting": "string",
  "paragraphs": ["string  // exactly 4"],
  "sign_off": "string"
}"""

COVER_USER = """=== CANDIDATE GROUND TRUTH ===
{profile}

=== TARGET ROLE ===
{title} at {company} ({location}, {work_mode})

Positioning angle: {angle}
Key requirements: {requirements}

Write the cover letter JSON."""
