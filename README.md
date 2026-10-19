# 🎯 Deep Job Search & Tailored Resume Generation Engine

An autonomous daily job-hunting system for **Ashwini** — Operations Transformation,
Continuous Improvement & Strategic Procurement.

It harvests remote/hybrid roles from public boards **and the hidden job market**
(company-owned ATS portals), scores each role 0–100 against a verified ground-truth
profile, and generates bespoke ATS-optimized resumes and cover letters using **free**
OpenRouter models with automatic discovery and failover.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# your OpenRouter key already lives in .env (chmod 600, git-ignored)
python -m jobhunter.cli doctor          # verify config, catalog, budget, deps

python -m jobhunter.cli daily           # full run: harvest → score → tailor
python -m jobhunter.cli serve           # dashboard at http://127.0.0.1:8080
```

> The system is **fully functional without an API key**. Harvesting, deduplication,
> the deterministic 0–100 scorer and the offline resume tailor all run locally. The
> LLM layer only adds deeper reasoning and richer prose.

### Running on a locked-down (managed) machine

Some corporate endpoint-security agents refuse to `mmap` compiled extension modules,
so any import of a Rust/C wheel fails with `dlopen ... errno=1`. FastAPI is affected,
because it depends on pydantic's compiled `pydantic_core`.

The dashboard therefore falls back to a pure-Python **Starlette** shim
(`jobhunter/web/_starlette_compat.py`) whenever FastAPI cannot be imported. Real
FastAPI is still preferred and used automatically when it is available — no
configuration needed. `doctor` reports which path is active:

```
fastapi      OK    (web dashboard, via starlette fallback)
```

Two further notes for such machines:

- Use a Python interpreter the agent permits. Anything **3.10+** works; the routes use
  `X | None` annotations, so **3.9 cannot serve the dashboard**.
- `python-jobspy` pins `numpy==1.26.3`, whose binaries are typically blocked too. It is
  optional — omit it and the pipeline degrades gracefully, losing only the
  LinkedIn/Indeed/Glassdoor connectors while all other sources keep working.

To get the blocks lifted rather than worked around, see [`security/`](security/) — it
holds a ready-to-send IT/InfoSec allowlist request, the diagnostic evidence behind it,
and `security/collect_blocked_binaries.sh`, which re-tests each binary and prints current
SHA-256 values for the ticket.

---

## 1 · Deep internet search & hidden-job extraction

**12 connectors, run concurrently.** Every source is fail-isolated — a broken board
never aborts the run.
| Tier | Sources |
|---|---|
| Public boards (open APIs) | RemoteOK, Remotive, Arbeitnow, Jobicy, Himalayas, WeWorkRemotely (RSS) |
| **Hidden market** (company ATS) | **Greenhouse** (18 boards), **Lever** (10), **Ashby** (7), **Workday CXS** (5 tenants) |
| **Startups** | **Y Combinator / Work at a Startup** (Operations, Finance, Product) |
| Aggregators (optional) | LinkedIn, Indeed via `python-jobspy` |

### Portal coverage and what actually works

Ranked by value for this candidate (company career pages first — lowest competition):

| Rank | Portal | Status |
|---|---|---|
| 1 | **Company career pages** | ✅ Greenhouse, Lever, Ashby, Workday — 40 portals |
| 2 | **Y Combinator** | ✅ Live via the public Inertia payload, no login |
| 3 | ZipRecruiter | ⚠️ HTTP 403 — Cloudflare blocks datacentre IPs |
| 4 | CutShort | ❌ Listings load from an API-key-protected endpoint |
| 5 | **LinkedIn** | ✅ Live via `python-jobspy` |
| 6 | Instahyre | ❌ Public API works but is ~99% engineering roles |
| 7 | Glassdoor | ⚠️ Returns 0 rows (anti-bot) |
| 8 | Naukri | ⚠️ HTTP 406 "recaptcha required" |

Blocked aggregators are listed under `jobspy.blocked_without_proxy` in
`config/search_matrix.yaml`. Add residential proxies to `jobspy.proxies` and move them
into `sites` to enable them — they are kept out of the active list by default because a
blocked site only costs time and never returns rows.

The hidden-market connectors call the same undocumented endpoints that companies'
own career pages use, surfacing roles that are frequently never syndicated to
LinkedIn or Indeed. In testing these produced **348 of 425 stored jobs**.

Configure targets in `config/search_matrix.yaml` (20 target roles, 14 keyword seeds,
work modes, locations, and the ATS company tokens).

**Pipeline:** harvest → normalize → cross-source dedupe (prefers hidden-market +
richest JD) → prefilter (work mode, reject levels) → hydrate stub JDs (Workday
second-pass fetch) → score → shortlist → tailor.

---

## 2 · Relevance & match scoring engine (0–100)

### Layer 1 — deterministic (free, explainable, runs on every job)

| Metric | Weight | How it is derived |
|---|---|---|
| Skill match | **45** | 18 pts title-family fit + 27 pts weighted competency coverage across a 12-competency graph with ~250 aliases |
| Seniority alignment | **20** | Title-level detection + years-of-experience extraction vs. the candidate's 3–8 yr sweet spot |
| Remote flexibility | **15** | Work-mode detection, location-preference match, geo-restriction penalties |
| Domain relevance | **12** | Coverage across 8 distinct domain *families* (prevents synonym farming) |
| Company growth stage | **8** | Funding/stage signals + hidden-market premium |

Plus penalties for off-profession titles, excluded keywords, reject-level seniority
and thin job descriptions.

Titles are classified as `core` / `adjacent` / `unrelated` / `off-target`, so a
"Staff Software Engineer" scores 5 while a "Procurement Portfolio Lead" scores 95.
Verified distribution over 394 real jobs:

```
90-99: 12    70-79: 59    50-59: 49    30-39: 38    10-19: 21
80-89: 48    60-69: 55    40-49: 62    20-29: 37     0-9:  13
```

### Layer 2 — LLM deep reasoning (shortlist only, **batched**)

Final score = `0.45 × deterministic + 0.55 × LLM`. Tiers: **A+** ≥82, **A** ≥70,
**B** ≥58, **C** below.

The LLM returns matched strengths, genuine gaps, ATS keywords to mirror, a
positioning angle and an apply/tailor/stretch/skip recommendation — all reused
later by the resume generator so no extra API call is ever needed.

---

## 3 · LLM provider chain: NVIDIA → OpenRouter → Cline

Every LLM call goes through `OpenRouterClient.chat()`, which tries providers in this
order so a capacity/rate limit on one never stalls a run:

1. **NVIDIA** (`NVIDIA_API_KEY`) — tried first. NVIDIA's hosted NIM catalog isn't
   rate-capped the way OpenRouter's free tier is, so a successful NVIDIA response
   short-circuits the whole OpenRouter path. If NVIDIA times out or errors, the
   client falls through automatically.
2. **OpenRouter** (`OPENROUTER_API_KEY`) — the discovery-driven, budget-aware router
   described below.
3. **Cline** (`CLINE_API_KEY`) — last-resort overflow. Triggered whenever OpenRouter's
   daily/run budget is exhausted or every model in its chain is rate-limited (429).

Each provider is a small, independent client (`jobhunter/llm/nvidia.py`,
`jobhunter/llm/cline.py`) with its own SQLite usage ledger and daily cap, so a
broken or exhausted provider can never block the others. Model lists and caps are
configured via env vars (`NVIDIA_MODELS`/`NVIDIA_DAILY_CAP`,
`CLINE_MODELS`/`CLINE_DAILY_CAP`) — leave blank to use the verified-working
defaults baked into each module.

### OpenRouter orchestration — nothing hardcoded

Model IDs are **discovered at runtime**, never pinned in code:

1. `openrouter.ai/api/frontend/v1/models/find?max_output_price=0` — richest payload
   (provider, rate limits, supported params, disabled/deranked flags)
2. `openrouter.ai/api/v1/models` — official API, filtered locally to price 0
3. If both endpoints are unavailable, use the last valid cache; otherwise skip LLM work
   and continue with deterministic scoring and offline tailoring.

Results are filtered to real **text→text chat** models (embedding, image, audio,
video, moderation and rerank models are excluded) and cached for 12 h.

### Per-task capability ranking

Each candidate is scored on family prior, parameter count, context window,
JSON/structured-output support, reasoning support, max output length, advertised
rate limits, and preview/deranked penalties — weighted differently per task:

```
parse  → JSON-mode ×4.0, speed ×1.6   (fast, high-volume extraction)
match  → reasoning ×3.0, size ×2.0    (deep candidate-to-role reasoning)
resume → size ×2.2, output_len ×1.8   (long-form ATS prose)
```

```bash
python -m jobhunter.cli models --task resume    # inspect the live ranking
```

### Reliability behaviours

- **Reasoning control** — free reasoning models will burn an entire completion budget
  on internal monologue and never reach the JSON. `reasoning.effort` + `exclude`
  caps the thinking; reasoning models also get a 3× token multiplier so thinking
  cannot starve the answer.
- **JSON failover** — a model that emits prose instead of JSON is *demoted* and the
  identical prompt is handed to the next model in the chain (verified live:
  `nemotron-3-super` → `gpt-oss-20b` recovered automatically).
- **429 cooldown** — rate-limited models are pushed to the back of the chain, never
  dropped, so a degraded run still completes.
- **response_format probing** — only sent to models that advertise it, and dropped
  automatically on a 400/404/422.
- **Tolerant parsing** — strips `<think>` blocks, markdown fences and trailing prose;
  accepts `{"results":[…]}`, bare lists, `{"J1":{…}}` and nested `sub_scores`.

---

## 4 · Efficient use of a limited API quota

Free OpenRouter keys are capped at **50 requests/day, 20/minute**. The engine treats
requests as a budgeted resource.

| Mechanism | Effect |
|---|---|
| **Batched triage** | 4 jobs scored per request instead of 1 |
| **Analysis reuse** | The batch result feeds resume tailoring — no separate parse call |
| **Persistent response cache** | Content-addressed; re-runs, re-scores and dashboard retries cost **0 requests** |
| **Daily ledger** | SQLite-persisted per-day / per-task call counter |
| **Task reservations** | `resume: 8, match: 6` held back so resume generation is never starved by triage |
| **Stage shares** | Each stage may only claim its configured fraction of the daily allowance |
| **Client-side RPM guard** | Sliding 60 s window keeps requests under the per-minute cap |
| **Auto-detected limits** | `GET /api/v1/key` determines free vs. paid tier; a 0.9 safety margin is applied |
| **Graceful degradation** | When the budget runs out mid-run, remaining resumes are built by the offline tailor rather than failing |

**Cost of a default daily run: ~13 requests** (2 batched triage + 5 resumes +
5 cover letters + overhead) — well within the 50/day cap, leaving headroom for
ad-hoc dashboard work.

```bash
python -m jobhunter.cli budget    # live ledger, cache stats, run-cost estimate
```

---

## 5 · ATS-optimized resume generation

Every document is built from `config/profile.yaml` — the single source of truth,
extracted from the real CV and IIM Udaipur resume.

**Anti-fabrication guarantees** (this is the part that matters):

1. **Employer/title/date lock** — any employer not in the ground truth is dropped;
   company, title and dates are forced back to the verified strings.
2. **Numeric truth enforcement** — every quantity in generated text must trace back
   to a ground-truth number. Unverifiable metrics are surgically excised, and the
   bullet is dropped if that fails. Verified live: the model's invented
   *"cutting turnaround time by 30%"*, *"raised OEE by 12%"* and
   *"reducing time-to-submit by 20%"* were all stripped, while the genuine
   *"₹110M AUM growth"*, *"15% retention"* and *"293+ assets"* were preserved.
3. **Structural cap** — a role cannot grow more bullets than the ground truth supports.
4. **Certification whitelist** — only certifications actually held may appear.
5. **Honest omission reporting** — JD keywords the candidate *cannot* truthfully claim
   are listed explicitly rather than fabricated.

**ATS-safe formatting:** the `.docx` and `.txt` submission formats stay single column,
with no tables/text boxes/images/headers, standard Calibri, parser-recognised section
headings, and plain hyphen bullets in the `.txt` variant.

**Six artifacts per job:** `.docx` (ATS submission), `.pdf` + `.html` (Material-themed
designed resume for human reviewers), `.md` (review + tailoring report), `_ATS.txt`
(paste into web forms), `.json` (machine-readable), plus an optional cover letter.
Achieved **88–90% ATS alignment** on live runs.

> Submit the `.docx` or `_ATS.txt` to applicant tracking systems. The `.pdf`/`.html`
> version is the visually designed copy for recruiters and human reviewers — it is
> deliberately two-column and styled, so it is not intended for ATS parsing.

**PDF engine:** the `.pdf` is produced by rendering the HTML in headless
Chrome/Chromium (auto-detected; override with `JOBHUNTER_CHROME=/path/to/chrome`),
falling back to WeasyPrint if installed. Both are optional — if neither is present the
`.pdf` is simply skipped and the `.html` can still be printed to PDF from a browser.
`python -m jobhunter.cli doctor` reports which engine was found.

After rendering, the PDF is read back with `pypdf` to (a) discard the file if a large
share of the text is missing — a truncated resume is worse than none — and (b) strip any
trailing blank page. Chrome typically fits this resume on 2 pages; WeasyPrint is slightly
looser. Do not set `overflow:hidden` or `display:flow-root` on `.body` in the print CSS:
both establish a block formatting context that WeasyPrint cannot fragment, silently
clipping the resume to one page.

## Running Ragini's Job Hunter (Port 8081)

To reuse the full engine for **Ragini Pathode** (Corporate Governance, Company Secretarial & Regulatory Compliance):

```bash
./run_ragini.sh
```

Or via environment variables:

```bash
export JOBHUNTER_PROFILE="config/profile_ragini.yaml"
export JOBHUNTER_SEARCH_MATRIX="config/search_matrix_ragini.yaml"
export JOBHUNTER_DATA_DIR="data_ragini"
export JOBHUNTER_DB="data_ragini/jobhunter.sqlite3"

python -m jobhunter.cli serve --port 8081
```

- **Ashwini's portal:** http://127.0.0.1:8080
- **Ragini's portal:** http://127.0.0.1:8081

---

## 6 · Web dashboard & daily automation

```bash
python -m jobhunter.cli serve            # FastAPI + APScheduler
python -m jobhunter.cli schedule --now   # scheduler only (07:30 Asia/Kolkata)
```

Dashboard: score-ranked match table with filters (tier, mode, source, hidden-only,
free-text), per-metric score breakdown bars, strengths/gaps/ATS keywords, the live
API-budget meter, the discovered model chains, source yields, model success rates,
and one-click resume/cover-letter generation with artifact downloads plus an in-browser
preview of the designed resume.

REST API: `/api/health` `/api/stats` `/api/jobs` `/api/jobs/{id}` `/api/run`
`/api/run/last` `/api/runs` `/api/jobs/{id}/rescore` `/api/jobs/{id}/tailor`
`/api/jobs/{id}/status` `/api/artifacts` `/api/config` `/api/llm/test` `/download`

---

## CLI reference

| Command | Purpose |
|---|---|
| `daily` | Full pipeline. `--deep N --resumes N --min-score N --no-cover --no-llm --only/--skip <sources>` |
| `search` | Harvest + score only |
| `list` | Browse stored jobs. `--min-score --tier --mode --source --query --hidden --json` |
| `tailor <job_id>` | Generate resume (+ cover letter) for one job. `--no-llm` for offline |
| `rescore <job_id>` | Re-run scoring |
| `models` | Live free-model catalog and per-task chains. `--task --refresh` |
| `budget` | API ledger, cache stats, run-cost estimate |
| `doctor` | Config, catalog, budget, dependency and connectivity check. `--no-live` |
| `sources` | Registered connectors |
| `stats` | Database statistics |
| `serve` / `schedule` | Dashboard / daily scheduler |

---

## Layout

```
config/
  profile.yaml         ← ground truth: competency graph, tagged bullets, seniority calibration
  search_matrix.yaml   ← target roles, keyword seeds, ATS company tokens
  models.json          ← routing POLICY (discovery, reasoning, budget) — no model IDs
jobhunter/
  config.py  models.py  db.py  scoring.py  pipeline.py  scheduler.py  cli.py
  llm/       catalog.py (discovery+ranking)  budget.py (ledger+cache)  openrouter.py  prompts.py
  sources/   base.py  boards.py  ats.py (hidden market)  jobspy_source.py
  resume/    tailor.py (synthesis+validation)  render.py (docx/md/txt)
  web/       app.py  templates/
data/        jobhunter.sqlite3  model_catalog.json  last_run.json  output/<date>/
```

## Configuration

`.env` (git-ignored, `chmod 600`) holds `OPENROUTER_API_KEY`, the schedule
(`JOBHUNTER_RUN_HOUR/MINUTE`, `JOBHUNTER_TZ`), run sizing (`JOBHUNTER_DEEP_N`,
`JOBHUNTER_RESUMES`, `JOBHUNTER_MIN_SCORE`) and budget overrides
(`JOBHUNTER_MAX_LLM_CALLS_PER_DAY/RUN`).

**Before applying:** replace the `email` and `phone` placeholders in
`config/profile.yaml` with real contact details.
