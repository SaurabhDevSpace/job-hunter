"""FastAPI dashboard + REST API for the deep job search engine."""
from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request as UrlRequest, urlopen

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import OUTPUT_DIR, load_model_config, load_profile, load_search_matrix
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
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
_scheduler = None
_custom_tailor_busy = threading.Lock()
_custom_tailor_tasks_lock = threading.Lock()
_custom_tailor_tasks: dict[str, dict[str, Any]] = {}


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
    out: list[dict[str, Any]] = []
    for row in rows:
        verdict, _ = eligible(str(row.get("location", "") or ""),
                              str(row.get("description", "") or ""))
        if verdict == "eligible":
            out.append(row)
    return out


def _clean_job_html(raw_html: str) -> tuple[str, str]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.I | re.S)
    title = re.sub(r"\s+", " ", (title_match.group(1) if title_match else "")).strip()
    text = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", raw_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    return title, text


def _fetch_job_posting(url: str) -> tuple[str, str]:
    req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0 (JobHunter)"})
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read(1_500_000).decode("utf-8", errors="ignore")
    except (HTTPError, URLError, TimeoutError) as exc:
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
    company = str(payload.get("company", "") or "").strip() or "Target Company"
    location = str(payload.get("location", "") or "").strip()
    use_llm = bool(payload.get("use_llm", True))

    fetched_title = ""
    if not desc and job_url:
        fetched_title, desc = _fetch_job_posting(job_url)
    if not desc or len(desc) < 80:
        raise HTTPException(400, "Provide a job URL or paste a fuller job description.")

    title = _guess_title(desc, fallback=title or fetched_title)
    url = job_url or "manual://pasted-jd"
    job = Job(source="manual", title=title, company=company, url=url,
              location=location, description=desc).normalize()

    from ..geo import eligible
    verdict, countries = eligible(job.location, job.description)
    if verdict != "eligible":
        raise HTTPException(
            400,
            f"Role is not India-permissible for remote/hybrid hiring (detected: {countries or ['unknown']}).",
        )

    pipe = Pipeline()
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
    root = OUTPUT_DIR.resolve()
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
