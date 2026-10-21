"""FastAPI dashboard + REST API for the deep job search engine."""
from __future__ import annotations

import html
import json
import logging
import os
import re
import ssl
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request as UrlRequest, urlopen

import requests

try:  # Real FastAPI when the platform allows it.
    from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    _HTTP_EXC_HANDLER = None
except ImportError:  # pragma: no cover - depends on the host's security policy
    # FastAPI needs pydantic's compiled core, which some hardened endpoints refuse
    # to load. Fall back to the pure-Python Starlette shim so the dashboard runs.
    from ._starlette_compat import (
        BackgroundTasks, FastAPI, FileResponse, HTMLResponse, HTTPException,
        Jinja2Templates, JSONResponse, Query, Request, StaticFiles,
        http_exception_handler as _HTTP_EXC_HANDLER,
    )

from ..config import (OUTPUT_DIR, load_model_config, load_profile,
                     load_search_matrix, output_dir)
from ..db import Store
from ..llm import LLMError, OpenRouterClient
from ..models import Job
from ..pipeline import Pipeline
from ..scoring import deterministic_score, llm_refine, parse_jd
from ..scheduler import build_scheduler, daily_job, is_running, last_report
from ..sources import REGISTRY

log = logging.getLogger("jobhunter.web")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Deep Job Search & Tailored Resume Portal", version="1.0.0")
if _HTTP_EXC_HANDLER is not None:
    app.add_exception_handler(HTTPException, _HTTP_EXC_HANDLER)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
_scheduler = None
_custom_tailor_busy = threading.Lock()
_custom_tailor_tasks_lock = threading.Lock()
_custom_tailor_tasks: dict[str, dict[str, Any]] = {}

CUSTOM_TAILOR_SOURCE = "My Own Search"


@app.on_event("startup")
def _startup() -> None:
    global _scheduler
    if os.getenv("JOBHUNTER_DISABLE_SCHEDULER", "").lower() in ("1", "true", "yes"):
        log.info("scheduler disabled by env")
        return
    hour = int(os.getenv("JOBHUNTER_RUN_HOUR", 7))
    minute = int(os.getenv("JOBHUNTER_RUN_MINUTE", 30))
    try:
        _scheduler = build_scheduler(hour, minute)
        _scheduler.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduler unavailable: %s", exc)


@app.on_event("shutdown")
def _shutdown() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)


def store() -> Store:
    return Store()


DOWNLOAD_LABELS = {
    "docx": "Resume (.docx)", "pdf": "Designed resume (.pdf)",
    "html": "Designed resume (.html)",
    "md": "Resume (.md)", "txt": "ATS text (.txt)",
    "json": "Resume data (.json)", "cover_docx": "Cover letter (.docx)",
    "cover_md": "Cover letter (.md)",
}


def _download_url(path: str | Path) -> str:
    return "/download?path=" + quote(str(path), safe="")


def _downloads(files: dict[str, Any]) -> list[dict[str, str]]:
    """Turn local artifact paths into ready-to-use download links for the UI."""
    out: list[dict[str, str]] = []
    for kind, path in (files or {}).items():
        p = Path(str(path))
        if not p.is_file():
            continue
        item = {"kind": kind, "label": DOWNLOAD_LABELS.get(kind, kind),
                "filename": p.name, "url": _download_url(p)}
        if p.suffix.lower() in (".html", ".pdf"):
            item["preview_url"] = _download_url(p) + "&inline=1"
        out.append(item)
    return out


def _india_permissible_jobs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from ..geo import eligible
    from ..scoring import classify_title
    profile = load_profile()
    out: list[dict[str, Any]] = []
    for row in rows:
        title = str(row.get("title", "") or "")
        family, _ = classify_title(title, profile)
        if family == "off-target":
            continue
        # Custom-tailor jobs are validated at ingestion; keep them visible in Matches.
        if str(row.get("source", "")) == CUSTOM_TAILOR_SOURCE:
            out.append(row)
            continue
        verdict, _ = eligible(str(row.get("location", "") or ""),
                              str(row.get("description", "") or ""))
        if verdict == "eligible":
            out.append(row)
    return out


def _clean_job_html(raw_html: str) -> tuple[str, str, str, str]:
    """Extract job details (title, description, company, location) from HTML or JSON-LD."""
    # 1. Look for Schema.org JobPosting in JSON-LD (Workday, Greenhouse, Lever, etc.)
    for block in re.findall(r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>', raw_html, re.I | re.S):
        try:
            data = json.loads(block.strip())
            items = data if isinstance(data, list) else data.get("@graph", [data])
            for item in items:
                if isinstance(item, dict) and (item.get("@type") == "JobPosting" or ("description" in item and "title" in item)):
                    t = html.unescape(str(item.get("title") or "")).strip()
                    desc_raw = str(item.get("description") or "")
                    desc_clean = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", desc_raw)
                    desc_clean = re.sub(r"(?s)<[^>]+>", " ", desc_clean)
                    desc_clean = html.unescape(desc_clean)
                    desc_clean = re.sub(r"\s+", " ", desc_clean).strip()

                    comp = ""
                    hiring_org = item.get("hiringOrganization")
                    if isinstance(hiring_org, dict):
                        comp = html.unescape(str(hiring_org.get("name") or "")).strip()
                    elif isinstance(hiring_org, str):
                        comp = html.unescape(hiring_org).strip()

                    loc = ""
                    job_loc = item.get("jobLocation")
                    if isinstance(job_loc, dict):
                        addr = job_loc.get("address")
                        if isinstance(addr, dict):
                            parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
                            loc = ", ".join(p for p in parts if p)
                    if t and desc_clean:
                        return t, desc_clean, comp, loc
        except Exception:
            continue

    # 2. Look for OpenGraph and meta description (common in SPAs and Oracle Cloud portals)
    og_title_match = re.search(r"""<meta[^>]+(?:property|name)=['"](?:og:title|twitter:title)['"][^>]+content=['"](.*?)['"]""", raw_html, flags=re.I)
    og_desc_match = re.search(r"""<meta[^>]+(?:property|name)=['"](?:og:description|description|twitter:description)['"][^>]+content=['"](.*?)['"]""", raw_html, flags=re.I)
    og_site_match = re.search(r"""<meta[^>]+(?:property|name)=['"](?:og:site_name)['"][^>]+content=['"](.*?)['"]""", raw_html, flags=re.I)

    meta_title = html.unescape(re.sub(r"\s+", " ", og_title_match.group(1)).strip()) if og_title_match else ""
    meta_desc = html.unescape(re.sub(r"\s+", " ", og_desc_match.group(1)).strip()) if og_desc_match else ""
    meta_comp = html.unescape(re.sub(r"\s+", " ", og_site_match.group(1)).strip()) if og_site_match else ""

    # 3. Fallback to standard HTML parsing
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.I | re.S)
    title = html.unescape(re.sub(r"\s+", " ", (title_match.group(1) if title_match else "")).strip())
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    chosen_title = meta_title or title
    chosen_desc = text if len(text) >= 80 else meta_desc
    return chosen_title, chosen_desc, meta_comp, ""


def _fetch_oracle_cloud_job(url: str) -> tuple[str, str, str, str] | None:
    """Extract requisition details from Oracle HCM Candidate Experience REST API."""
    try:
        parsed = urlparse(url)
        if "oraclecloud.com" not in (parsed.netloc or "").lower():
            return None
        m = re.search(r"/job(?:s)?/([^/?#]+)", parsed.path)
        if not m:
            return None
        req_id = m.group(1)
        api_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails/{req_id}?expand=all"
        resp = requests.get(api_url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (JobHunter)"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        title = html.unescape(str(data.get("Title") or "").strip())
        desc_parts = [
            data.get("ExternalDescriptionStr"),
            data.get("ExternalResponsibilitiesStr"),
            data.get("ExternalQualificationsStr"),
            data.get("ShortDescriptionStr"),
        ]
        raw_combined = " ".join(str(p) for p in desc_parts if p)
        desc_clean = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_combined)
        desc_clean = re.sub(r"(?s)<[^>]+>", " ", desc_clean)
        desc_clean = html.unescape(desc_clean)
        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()

        comp = str(data.get("LegalEmployer") or data.get("BusinessUnit") or data.get("Organization") or "").strip()
        if not comp:
            try:
                page_resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (JobHunter)"})
                if page_resp.status_code == 200:
                    site_match = re.search(r"""<meta[^>]+(?:property|name)=['"](?:og:site_name)['"][^>]+content=['"](.*?)['"]""", page_resp.text, flags=re.I)
                    if site_match:
                        comp = html.unescape(re.sub(r"\s+", " ", site_match.group(1)).strip())
            except Exception:
                pass
        loc = str(data.get("PrimaryLocation") or "").strip()
        if desc_clean and len(desc_clean) >= 80:
            return title, desc_clean, comp, loc
    except Exception as exc:
        log.warning("Oracle Cloud API extraction failed for %s: %s", url, exc)
    return None


def _ssl_context() -> ssl.SSLContext | None:
    """TLS context that works even when the interpreter has no CA bundle.

    python.org builds ship without root certificates until the bundled
    "Install Certificates.command" is run, which needs admin rights. ``requests``
    is unaffected because it always uses ``certifi``, so fall back to the same
    bundle here rather than failing every fetch with CERTIFICATE_VERIFY_FAILED.
    """
    default = ssl.get_default_verify_paths().openssl_cafile
    if default and os.path.exists(default):
        return None  # System store is fine; let urlopen use its default.
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def _fetch_job_posting(url: str) -> tuple[str, str, str, str]:
    # Check specialized ATS APIs first
    oracle_res = _fetch_oracle_cloud_job(url)
    if oracle_res is not None:
        return oracle_res

    # Try requests first (handles certifi, connection pooling, and SSL natively)
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (JobHunter)"})
        if resp.status_code < 400 and resp.text:
            return _clean_job_html(resp.text)
    except Exception as exc:
        log.debug("requests failed to fetch %s: %s; falling back to urlopen", url, exc)

    req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0 (JobHunter)"})
    try:
        with urlopen(req, timeout=20, context=_ssl_context()) as resp:
            raw = resp.read(1_500_000).decode("utf-8", errors="ignore")
    except (HTTPError, URLError, TimeoutError, ssl.SSLError) as exc:
        raise HTTPException(400, f"Could not fetch job URL: {exc}") from exc
    return _clean_job_html(raw)


def _guess_title(description: str, fallback: str = "") -> str:
    if fallback.strip():
        return fallback.strip()
    for line in re.split(r"[\r\n]+", description):
        s = re.sub(r"\s+", " ", line).strip()
        if len(s) >= 6:
            return s[:140]
    return "Target Role"


def _tailor_custom(payload: dict[str, Any]) -> dict[str, Any]:
    """Build one custom resume outside the request-response lifecycle."""
    job_url = str(payload.get("job_url", "") or "").strip()
    desc = str(payload.get("job_description", "") or "").strip()
    title = str(payload.get("title", "") or "").strip()
    company = str(payload.get("company", "") or "").strip()
    location = str(payload.get("location", "") or "").strip()
    use_llm = bool(payload.get("use_llm", True))
    force = bool(payload.get("force", True))  # Default True for user-initiated custom tailoring

    fetched_title = ""
    if not desc and job_url:
        fetched_title, desc, fetched_comp, fetched_loc = _fetch_job_posting(job_url)
        if not company:
            company = fetched_comp
        if not location:
            location = fetched_loc
    if not company:
        company = "Target Company"
    if not desc or len(desc) < 80:
        raise HTTPException(400, "Provide a job URL or paste a fuller job description.")

    title = _guess_title(desc, fallback=title or fetched_title)
    url = job_url or "manual://pasted-jd"
    job = Job(source="My Own Search", title=title, company=company, url=url,
              location=location, description=desc).normalize()

    from ..geo import eligible
    verdict, countries = eligible(job.location, job.description)
    if verdict != "eligible" and not force:
        raise HTTPException(
            400,
            f"Role is not India-permissible for remote/hybrid hiring (detected: {countries or ['unknown']}). Pass force=true to proceed.",
        )

    pipe = Pipeline()
    if use_llm and pipe.client.enabled:
        # Custom tailoring should finish promptly on interactive API calls even
        # when providers are overloaded. Keep retries bounded for this endpoint
        # only; normal daily runs keep the default reliability policy.
        retry_cfg = pipe.client.cfg.raw.setdefault("retry", {})
        discovery_cfg = pipe.client.cfg.raw.setdefault("discovery", {})
        retry_cfg["timeout_seconds"] = max(
            10, min(180, int(os.getenv("JOBHUNTER_CUSTOM_LLM_TIMEOUT_SECONDS", "35")))
        )
        retry_cfg["max_attempts_per_model"] = max(
            1, min(3, int(os.getenv("JOBHUNTER_CUSTOM_LLM_ATTEMPTS_PER_MODEL", "1")))
        )
        discovery_cfg["chain_length"] = max(
            1, min(5, int(os.getenv("JOBHUNTER_CUSTOM_LLM_CHAIN_LENGTH", "2")))
        )

    pipe.store.upsert_jobs([job])
    pipe.store.collapse_duplicates()
    pipe._canonical = pipe.store.canonical_map([job.dedupe_key])  # noqa: SLF001

    score = deterministic_score(job, pipe.profile)
    parsed: dict[str, Any] = {}
    if use_llm and pipe.client.enabled:
        try:
            parsed, _ = parse_jd(pipe.client, job)
        except LLMError:
            pass
        # Reuse this single parsed JD for scoring and resume generation.
        score = llm_refine(pipe.client, job, pipe.profile, score, parsed=parsed)
    score.job_id = pipe.cid(job)
    pipe.store.save_score(score)

    out = pipe.tailor(job, score, with_cover_letter=False, use_llm=use_llm, analysis=parsed)
    resume = out["resume"]

    # Replicate custom tailoring to Turso cloud DB
    try:
        from ..turso import push_to_turso
        with pipe.store.conn() as c:
            push_to_turso(c)
    except Exception:
        pass

    return {
        "job_id": score.job_id,
        "generated_by": resume.generated_by,
        "ats_alignment": resume.ats_alignment_score,
        "downloads": _downloads(out["files"]),
        "resume": resume.to_dict(),
        "files": out["files"],
        "source": "custom",
    }


def _run_custom_tailor(task_id: str, payload: dict[str, Any]) -> None:
    try:
        result = _tailor_custom(payload)
    except HTTPException as exc:
        error = str(exc.detail)
        log.info("custom tailoring %s rejected: %s", task_id, error)
        with _custom_tailor_tasks_lock:
            _custom_tailor_tasks[task_id].update(status="failed", error=error)
    except Exception:
        log.exception("custom tailoring %s failed", task_id)
        with _custom_tailor_tasks_lock:
            _custom_tailor_tasks[task_id].update(
                status="failed", error="Resume generation failed. Please try again.")
    else:
        with _custom_tailor_tasks_lock:
            _custom_tailor_tasks[task_id].update(status="completed", result=result)
    finally:
        _custom_tailor_busy.release()


# ------------------------------------------------------------------- views
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request,
              min_score: float = Query(0), tier: str | None = None,
              mode: str | None = None, source: str | None = None,
              q: str | None = None, hidden: bool = False, limit: int = 120):
    st = store()
    jobs = st.list_jobs(min_score=min_score, tier=tier, work_mode=mode, source=source,
                        hidden_only=hidden, query=q, limit=limit)
    jobs = _india_permissible_jobs(jobs)
    cfg = load_model_config()
    client = OpenRouterClient(cfg)
    stats = st.stats()
    # Filter dropdown must reflect the *actual* distinct source values stored on jobs
    # (e.g. jobspy fans out into "linkedin"/"indeed"/"glassdoor"), not the connector
    # registry keys — those don't line up 1:1 with what ends up in the DB.
    db_sources = sorted({row["source"] for row in stats["sources"] if row["source"]})
    return TEMPLATES.TemplateResponse(request, "dashboard.html", {
        "profile": load_profile(),
        "jobs": jobs,
        "match_count": len(jobs),
        "budget": client.budget_state() if client.enabled else None,
        "stats": stats,
        "runs": st.runs(8),
        "artifacts": st.artifacts(limit=25),
        "last_report": last_report(),
        "running": is_running(),
        "llm_ready": bool(cfg.api_key),
        "model_chain": {t: cfg.chain_for(t)[:4] for t in ("parse", "match", "resume")},
        "sources": db_sources,
        "filters": {"min_score": min_score, "tier": tier or "", "mode": mode or "",
                    "source": source or "", "q": q or "", "hidden": hidden, "limit": limit},
    })


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    st = store()
    job = st.job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return TEMPLATES.TemplateResponse(request, "job.html", {
        "job": job, "artifacts": st.artifacts(job_id), "profile": load_profile(),
    })


# --------------------------------------------------------------- REST API
def _nvidia_state() -> dict[str, Any]:
    try:
        from ..llm.nvidia import NvidiaClient
        return NvidiaClient().state()
    except Exception:
        return {"configured": False, "usable": False}


def _cline_state() -> dict[str, Any]:
    try:
        from ..llm.cline import ClineClient
        return ClineClient().state()
    except Exception:
        return {"configured": False, "usable": False}


@app.get("/api/health")
def health() -> dict[str, Any]:
    cfg = load_model_config()
    return {"status": "ok", "llm_configured": bool(cfg.api_key),
            "nvidia_configured": _nvidia_state().get("configured", False),
            "cline_configured": _cline_state().get("configured", False),
            "running": is_running(), "sources": list(REGISTRY)}


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    return store().stats()


@app.get("/api/jobs")
def api_jobs(min_score: float = 0, tier: str | None = None, mode: str | None = None,
             source: str | None = None, hidden: bool = False, q: str | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
    rows = store().list_jobs(min_score=min_score, tier=tier, work_mode=mode, source=source,
                             hidden_only=hidden, query=q, limit=limit)
    return _india_permissible_jobs(rows)


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict[str, Any]:
    job = store().job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {**job, "artifacts": store().artifacts(job_id)}


@app.post("/api/run")
def api_run(background: BackgroundTasks, deep: int = 12, resumes: int = 5,
            min_score: float = 60.0, use_llm: bool = True) -> dict[str, Any]:
    if is_running():
        return {"started": False, "reason": "a run is already in progress"}
    background.add_task(daily_job, deep_top_n=deep, resume_top_n=resumes,
                        min_score=min_score, use_llm=use_llm)
    return {"started": True, "message": "daily deep search started in background"}


@app.get("/api/run/last")
def api_last_run() -> dict[str, Any]:
    return last_report() or {"message": "no run recorded yet"}


@app.get("/api/run/status")
def api_run_status() -> dict[str, Any]:
    return {"running": is_running(), "last_report": last_report()}


@app.get("/api/runs")
def api_runs(limit: int = 20) -> list[dict[str, Any]]:
    return store().runs(limit)


@app.post("/api/jobs/{job_id}/rescore")
def api_rescore(job_id: str, use_llm: bool = True) -> dict[str, Any]:
    try:
        return Pipeline().rescore_existing(job_id, use_llm=use_llm).to_dict()
    except KeyError:
        raise HTTPException(404, "job not found")


@app.post("/api/jobs/{job_id}/tailor")
def api_tailor(job_id: str, use_llm: bool = True, cover_letter: bool = True) -> dict[str, Any]:
    try:
        out = Pipeline().tailor_existing(job_id, use_llm=use_llm, with_cover_letter=cover_letter)
    except KeyError:
        raise HTTPException(404, "job not found")
    except LLMError as exc:
        raise HTTPException(502, str(exc))
    resume = out["resume"]
    return {
        "job_id": job_id,
        "generated_by": resume.generated_by,
        "ats_alignment": resume.ats_alignment_score,
        "downloads": _downloads(out["files"]),
        "resume": resume.to_dict(),
        "files": out["files"],
        "cover_letter": out["cover_letter"],
    }


@app.post("/api/tailor/custom")
async def api_tailor_custom(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    payload = await request.json()
    if not _custom_tailor_busy.acquire(blocking=False):
        return {"started": False, "reason": "a custom resume is already being generated"}

    task_id = uuid.uuid4().hex
    with _custom_tailor_tasks_lock:
        _custom_tailor_tasks.clear()
        _custom_tailor_tasks[task_id] = {"status": "running"}
    background.add_task(_run_custom_tailor, task_id, payload)
    return {"started": True, "task_id": task_id,
            "message": "custom resume generation started"}


@app.get("/api/tailor/custom/{task_id}")
def api_tailor_custom_status(task_id: str) -> dict[str, Any]:
    with _custom_tailor_tasks_lock:
        task = _custom_tailor_tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "custom tailoring task not found")
        return {"task_id": task_id, **task}


@app.post("/api/jobs/{job_id}/status")
def api_status(job_id: str, status: str) -> dict[str, Any]:
    if status not in {"new", "reviewed", "tailored", "applied", "rejected", "archived"}:
        raise HTTPException(400, "invalid status")
    store().set_status(job_id, status)
    return {"job_id": job_id, "status": status}


@app.get("/api/artifacts")
def api_artifacts(job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    rows = []
    for a in store().artifacts(job_id, limit):
        row = {**a, "download_url": _download_url(a["path"])}
        if str(a["path"]).lower().endswith((".html", ".pdf")):
            row["preview_url"] = row["download_url"] + "&inline=1"
        rows.append(row)
    return rows


@app.get("/download")
def download(path: str, inline: bool = False):
    """Serve generated artifacts. Restricted to the output directory."""
    target = Path(path).resolve()
    root = output_dir().resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(403, "forbidden")
    if inline and target.suffix.lower() == ".html":
        # Preview the designed resume in-browser (print to PDF from there).
        # Content is escaped at render time; CSP blocks any script/remote asset anyway.
        return HTMLResponse(
            target.read_text(encoding="utf-8"),
            headers={"Content-Security-Policy":
                     "default-src 'none'; style-src 'unsafe-inline'; img-src data:"},
        )
    if inline and target.suffix.lower() == ".pdf":
        # Let the browser's built-in viewer render it instead of downloading.
        return FileResponse(
            str(target), media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{target.name}"'},
        )
    return FileResponse(str(target), filename=target.name)


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    cfg = load_model_config()
    matrix = load_search_matrix()
    return {
        "llm_configured": bool(cfg.api_key),
        "provider_order": ["nvidia", "openrouter", "cline"],
        "nvidia": _nvidia_state(),
        "cline": _cline_state(),
        "site_name": cfg.site_name,
        "chains": {t: cfg.chain_for(t) for t in ("parse", "match", "resume")},
        "target_roles": matrix.get("target_roles", []),
        "work_modes": matrix.get("work_modes", []),
        "hidden_portals": {k: len(v) for k, v in (matrix.get("ats") or {}).items()},
        "sources": list(REGISTRY),
    }


@app.get("/api/llm/test")
def api_llm_test() -> JSONResponse:
    client = OpenRouterClient(load_model_config())
    # NVIDIA/Cline can serve this call even when OpenRouter itself isn't configured —
    # chat() tries NVIDIA first and falls back to Cline, so only bail out early if
    # none of the three providers are usable.
    if not client.enabled and not _nvidia_state().get("usable") and not _cline_state().get("usable"):
        return JSONResponse(
            {"ok": False, "error": "No LLM provider configured (OPENROUTER_API_KEY, "
                                    "NVIDIA_API_KEY, CLINE_API_KEY all unset)."},
            status_code=400)
    try:
        r = client.chat("parse", [{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=12)
        return JSONResponse({"ok": True, "model": r.model, "response": r.text.strip()[:80]})
    except LLMError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
