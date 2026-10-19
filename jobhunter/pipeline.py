"""Daily orchestration: harvest -> dedupe -> score -> shortlist -> tailor resumes."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (OUTPUT_DIR, Profile, ensure_dirs, load_model_config,
                     load_profile, load_search_matrix)
from .db import Store
from .llm import LLMBudgetError, LLMError, OpenRouterClient
from .llm import prompts as P
from .models import Job, JobScore, ScoreBreakdown
from .resume import build_resume_payload, render_all
from .resume.render import output_paths, render_cover_letter_docx, render_cover_letter_md
from .scoring import batch_llm_refine, deterministic_score, llm_refine, parse_jd
from .sources import build_sources
from .sources.ats import WorkdaySource

log = logging.getLogger("jobhunter.pipeline")


@dataclass
class RunReport:
    run_id: int = 0
    jobs_found: int = 0
    jobs_after_dedupe: int = 0
    jobs_new: int = 0
    jobs_scored: int = 0
    resumes_built: int = 0
    sources_ok: dict[str, int] = field(default_factory=dict)
    sources_failed: dict[str, str] = field(default_factory=dict)
    shortlist: list[dict[str, Any]] = field(default_factory=list)
    llm_enabled: bool = False
    budget: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "jobs_found": self.jobs_found,
            "jobs_after_dedupe": self.jobs_after_dedupe, "jobs_new": self.jobs_new,
            "jobs_scored": self.jobs_scored, "resumes_built": self.resumes_built,
            "sources_ok": self.sources_ok, "sources_failed": self.sources_failed,
            "llm_enabled": self.llm_enabled, "budget": self.budget,
            "shortlist": self.shortlist, "errors": self.errors,
        }


def _shortlist_entry(job: Job, score: JobScore, out: dict[str, Any],
                     job_id: str | None = None) -> dict[str, Any]:
    return {
        "job_id": job_id or job.job_id, "title": job.title, "company": job.company,
        "url": job.url, "source": job.source, "hidden": job.is_hidden,
        "work_mode": job.work_mode, "location": job.location,
        "match_score": score.match_score, "tier": score.tier,
        "breakdown": score.breakdown.to_dict(), "rationale": score.llm_rationale,
        "ats_alignment": out["resume"].ats_alignment_score,
        "generated_by": out["resume"].generated_by,
        "files": out["files"],
    }


class Pipeline:
    def __init__(self, store: Store | None = None, profile: Profile | None = None,
                 matrix: dict[str, Any] | None = None, client: OpenRouterClient | None = None):
        ensure_dirs()
        self.store = store or Store()
        self.profile = profile or load_profile()
        self.matrix = matrix or load_search_matrix()
        self.client = client or OpenRouterClient(load_model_config(), logger=self.store.log_llm)
        # job_id -> LLM analysis from the batched scoring pass, reused when tailoring
        self._analyses: dict[str, dict[str, Any]] = {}
        # dedupe_key -> stored job_id, so merged postings write to the surviving row
        self._canonical: dict[str, str] = {}

    def cid(self, job: Job) -> str:
        """The job_id actually stored for this posting (may differ after a merge)."""
        return self._canonical.get(job.dedupe_key, job.job_id)

    # ------------------------------------------------------------- HARVEST
    def harvest(self, only: list[str] | None = None, skip: list[str] | None = None,
                max_workers: int = 8) -> tuple[list[Job], dict[str, int], dict[str, str]]:
        sources = build_sources(only=only, skip=skip)
        jobs: list[Job] = []
        ok: dict[str, int] = {}
        failed: dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(s.safe_fetch, self.matrix): s for s in sources}
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    found, err = fut.result()
                except Exception as exc:  # noqa: BLE001
                    failed[src.name] = f"{type(exc).__name__}: {exc}"
                    continue
                if err:
                    failed[src.name] = err
                ok[src.name] = len(found)
                jobs.extend(found)
                log.info("source %-16s -> %3d jobs", src.name, len(found))

        return jobs, ok, failed

    # -------------------------------------------------------------- DEDUPE
    def dedupe(self, jobs: list[Job]) -> list[Job]:
        """Collapse cross-source duplicates, preferring hidden-market + richest JD."""
        best: dict[str, Job] = {}
        for job in jobs:
            key = job.dedupe_key
            cur = best.get(key)
            if cur is None:
                best[key] = job
                continue
            if _quality(job) > _quality(cur):
                best[key] = job
        return list(best.values())

    def prefilter(self, jobs: list[Job]) -> list[Job]:
        """Drop obvious non-fits before any LLM spend — including geo-ineligible roles and off-target titles."""
        from jobhunter.geo import eligible
        from jobhunter.scoring import classify_title
        prefs = self.profile.preferences
        allowed_modes = set(prefs.get("work_modes", ["remote", "hybrid"])) | {"unknown"}
        reject = [r.lower() for r in self.profile.seniority.get("reject_levels", [])]
        out = []
        for job in jobs:
            if job.work_mode not in allowed_modes:
                continue
            # Drop completely off-target professions (software/developer/coding/engineering for non-tech roles)
            family, _ = classify_title(job.title, self.profile)
            if family == "off-target":
                continue
            # India work-authorization gate
            verdict, _ = eligible(job.location, job.description)
            if verdict != "eligible":
                continue
            tl = job.title.lower()
            if any(f" {r} " in f" {tl} " or tl.startswith(r) for r in reject if len(r) > 3):
                continue
            out.append(job)
        return out

    def hydrate(self, jobs: list[Job], max_workers: int = 6) -> list[Job]:
        """Second-pass JD fetch for sources that only return stubs (Workday)."""
        need = [j for j in jobs if j.source == "workday" and len(j.description) < 400]
        if not need:
            return jobs
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(WorkdaySource.hydrate, need))
        return jobs

    # --------------------------------------------------------------- SCORE
    def score_jobs(self, jobs: list[Job], deep_top_n: int = 12,
                   use_llm: bool = True) -> list[tuple[Job, JobScore]]:
        """Deterministic score for everything; batched LLM deep-score for the top N.

        The batched pass returns a reusable per-job analysis, which is cached on the
        pipeline so resume tailoring never needs a separate 'parse' request.
        """
        scored = [(j, deterministic_score(j, self.profile)) for j in jobs]
        scored.sort(key=lambda t: t[1].match_score, reverse=True)

        if use_llm and self.client.enabled and deep_top_n > 0:
            policy = self.client.cfg.budget
            batch_size = int(policy.get("batch_match_size", 6)) \
                if policy.get("batch_enabled", True) else 1
            shortlist = scored[:deep_top_n]
            wanted_calls = -(-len(shortlist) // max(1, batch_size))
            allowed_calls = self.client.budget.budget_for("match", wanted_calls)
            if allowed_calls < wanted_calls:
                log.info("budget: deep-scoring %d/%d batches (%d calls available)",
                         allowed_calls, wanted_calls, allowed_calls)
            try:
                analyses = batch_llm_refine(self.client, shortlist, self.profile,
                                            batch_size=batch_size, max_calls=allowed_calls)
                self._analyses.update(analyses)
            except Exception as exc:  # noqa: BLE001
                log.warning("batched deep scoring failed: %s", exc)
            scored.sort(key=lambda t: t[1].match_score, reverse=True)

        for job, score in scored:
            score.job_id = self.cid(job)
            self.store.save_score(score)
        return scored

    # ------------------------------------------------------------- TAILOR
    def tailor(self, job: Job, score: JobScore, with_cover_letter: bool = True,
               use_llm: bool = True, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
        # Reuse the analysis produced by the batched scoring pass — a separate
        # JD-parse request would cost budget for information we already hold.
        parsed: dict[str, Any] = analysis or self._analyses.get(job.job_id) or {}
        job_id = self.cid(job)

        resume = build_resume_payload(
            job, self.profile, score,
            client=self.client if (use_llm and self.client.enabled) else None,
            parsed=parsed,
        )
        written = render_all(resume, self.profile, job)
        for kind, key in (("resume_docx", "docx"), ("resume_pdf", "pdf"),
                          ("resume_html", "html"),
                          ("resume_md", "md"), ("ats_txt", "txt"), ("resume_json", "json")):
            path = written.get(key)
            if path is None:  # e.g. pdf when no browser/WeasyPrint is installed
                continue
            self.store.add_artifact(job_id, kind, str(path),
                                    resume.generated_by, resume.ats_alignment_score)

        cover: dict[str, Any] | None = None
        if with_cover_letter and use_llm and self.client.enabled:
            cover = self._cover_letter(job, score)
            if cover:
                paths = output_paths(job)
                render_cover_letter_docx(cover, self.profile, job, paths["cover_docx"])
                render_cover_letter_md(cover, self.profile, job, paths["cover_md"])
                self.store.add_artifact(job_id, "cover_letter", str(paths["cover_docx"]),
                                        resume.generated_by)
                written["cover_docx"] = paths["cover_docx"]

        self.store.set_status(job_id, "tailored")
        return {"resume": resume, "files": {k: str(v) for k, v in written.items()},
                "cover_letter": cover}

    def _cover_letter(self, job: Job, score: JobScore) -> dict[str, Any] | None:
        user = P.COVER_USER.format(
            profile=self.profile.as_llm_context(),
            title=job.title, company=job.company,
            location=job.location or "n/a", work_mode=job.work_mode,
            angle=score.llm_rationale or "Operations & procurement transformation specialist.",
            requirements=", ".join(score.ats_keywords[:14]),
        )
        try:
            data, _ = self.client.json_call("resume", P.COVER_SYSTEM, user,
                                           schema_hint=P.COVER_SCHEMA, max_tokens=1400)
            return data if isinstance(data, dict) and data.get("paragraphs") else None
        except LLMError as exc:
            log.info("cover letter failed for %s: %s", job.job_id, exc)
            return None

    # ----------------------------------------------------------- FULL RUN
    def run_daily(self, *, deep_top_n: int = 12, resume_top_n: int = 5,
                  min_score: float = 60.0, use_llm: bool = True,
                  only: list[str] | None = None, skip: list[str] | None = None,
                  with_cover_letter: bool = True) -> RunReport:
        report = RunReport(llm_enabled=bool(use_llm and self.client.enabled))
        report.run_id = self.store.start_run()
        self._analyses.clear()
        run_failed = False
        if report.llm_enabled:
            self.client.budget.reset_run()
            st = self.client.budget.state()
            log.info("=== daily run %s started | API budget: %d/%d used today, "
                     "%d available this run ===", report.run_id, st.used_today,
                     st.daily_limit, min(st.remaining_today, st.run_limit))
        else:
            log.info("=== daily run %s started (LLM off — deterministic mode) ===", report.run_id)

        try:
            raw, ok, failed = self.harvest(only=only, skip=skip)
            report.jobs_found, report.sources_ok, report.sources_failed = len(raw), ok, failed

            jobs = self.prefilter(self.dedupe(raw))
            jobs = self.hydrate(jobs)
            report.jobs_after_dedupe = len(jobs)

            total, new = self.store.upsert_jobs(jobs)
            report.jobs_new = new
            merged = self.store.collapse_duplicates()
            self._canonical = self.store.canonical_map(j.dedupe_key for j in jobs)
            log.info("%d jobs after dedupe/prefilter (%d new%s)", total, new,
                     f", {merged} duplicate rows merged" if merged else "")

            scored = self.score_jobs(jobs, deep_top_n=deep_top_n, use_llm=use_llm)
            report.jobs_scored = len(scored)

            shortlist = [(j, s) for j, s in scored if s.match_score >= min_score][:resume_top_n]
            if not shortlist:  # never return empty-handed — take the best available
                shortlist = scored[:min(resume_top_n, len(scored))]

            # Budget allocation for tailoring: 1 request per resume, +1 per cover letter.
            llm_resumes = len(shortlist)
            if report.llm_enabled:
                per_job = 2 if with_cover_letter else 1
                llm_resumes = self.client.budget.budget_for("resume", len(shortlist) * per_job) // per_job
                if llm_resumes < len(shortlist):
                    log.info("budget: %d/%d resumes will use the LLM; the rest fall back to the "
                             "offline deterministic tailor", llm_resumes, len(shortlist))

            for idx, (job, score) in enumerate(shortlist):
                job_use_llm = use_llm and idx < llm_resumes
                try:
                    out = self.tailor(job, score, use_llm=job_use_llm,
                                      with_cover_letter=with_cover_letter and job_use_llm)
                    report.resumes_built += 1
                    report.shortlist.append(_shortlist_entry(job, score, out, self.cid(job)))
                except LLMBudgetError as exc:
                    # Out of API calls: finish the remaining resumes offline rather than fail.
                    log.info("budget exhausted mid-run (%s) — switching to offline tailoring", exc)
                    llm_resumes = 0
                    try:
                        out = self.tailor(job, score, use_llm=False, with_cover_letter=False)
                        report.resumes_built += 1
                        report.shortlist.append(_shortlist_entry(job, score, out, self.cid(job)))
                    except Exception as inner:  # noqa: BLE001
                        report.errors.append(f"offline tailor failed for {job.company}: {inner}")
                except Exception as exc:  # noqa: BLE001
                    msg = f"tailor failed for {job.company} / {job.title}: {exc}"
                    log.error(msg, exc_info=True)
                    report.errors.append(msg)

            if report.llm_enabled:
                report.budget = self.client.budget_state()
            return report
        except BaseException as exc:
            run_failed = True
            report.errors.append(f"run aborted: {type(exc).__name__}: {exc}")
            raise
        finally:
            self.store.finish_run(
                report.run_id, sources_ok=report.sources_ok, sources_failed=report.sources_failed,
                jobs_found=report.jobs_found, jobs_new=report.jobs_new,
                jobs_scored=report.jobs_scored, resumes_built=report.resumes_built,
                status="failed" if run_failed else "completed",
                notes="; ".join(report.errors)[:1000] or None,
            )
            log.info("=== daily run %s finished: %d found, %d new, %d resumes ===",
                     report.run_id, report.jobs_found, report.jobs_new, report.resumes_built)

    # ------------------------------------------------------------ helpers
    def job_from_row(self, row: dict[str, Any]) -> Job:
        return Job(
            source=row["source"], title=row["title"], company=row["company"], url=row["url"],
            job_id=row["job_id"],
            description=row.get("description") or "", location=row.get("location") or "",
            work_mode=row.get("work_mode") or "unknown", salary=row.get("salary") or "",
            posted_at=row.get("posted_at") or "", department=row.get("department") or "",
            employment_type=row.get("employment_type") or "",
        )

    def score_from_row(self, row: dict[str, Any]) -> JobScore:
        bd = row.get("breakdown") or {}
        return JobScore(
            job_id=row["job_id"],
            match_score=float(row.get("match_score") or 0),
            breakdown=ScoreBreakdown(**{k: float(v) for k, v in bd.items()}) if bd else ScoreBreakdown(),
            matched_competencies=row.get("matched_competencies") or [],
            missing_keywords=row.get("missing_keywords") or [],
            ats_keywords=row.get("ats_keywords") or [],
            seniority_detected=row.get("seniority_detected") or "unknown",
            growth_stage=row.get("growth_stage") or "unknown",
            title_family=row.get("title_family") or "unknown",
            tier=row.get("tier") or "C",
            llm_score=row.get("llm_score"),
            llm_rationale=row.get("llm_rationale") or "",
            llm_model=row.get("llm_model") or "",
        )

    def tailor_existing(self, job_id: str, use_llm: bool = True,
                        with_cover_letter: bool = True) -> dict[str, Any]:
        row = self.store.job(job_id)
        if not row:
            raise KeyError(f"unknown job_id {job_id}")
        job = self.job_from_row(row)
        score = (self.score_from_row(row) if row.get("match_score") is not None
                 else deterministic_score(job, self.profile))
        if row.get("match_score") is None:
            self.store.save_score(score)
        return self.tailor(job, score, with_cover_letter=with_cover_letter, use_llm=use_llm)

    def rescore_existing(self, job_id: str, use_llm: bool = True) -> JobScore:
        row = self.store.job(job_id)
        if not row:
            raise KeyError(f"unknown job_id {job_id}")
        job = self.job_from_row(row)
        score = deterministic_score(job, self.profile)
        if use_llm and self.client.enabled:
            try:
                parsed, _ = parse_jd(self.client, job)
            except LLMError:
                parsed = {}
            llm_refine(self.client, job, self.profile, score, parsed)
        self.store.save_score(score)
        return score


def _quality(job: Job) -> tuple[int, int, int]:
    """Preference order when collapsing duplicates."""
    return (1 if job.is_hidden else 0, len(job.description), 1 if job.work_mode != "unknown" else 0)
