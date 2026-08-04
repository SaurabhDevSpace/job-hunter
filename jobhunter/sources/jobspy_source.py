"""LinkedIn / Indeed / Glassdoor / Google Jobs via the `python-jobspy` scraper.

Optional dependency: `pip install python-jobspy`. If it is not installed the
source degrades gracefully and the rest of the pipeline is unaffected.
"""
from __future__ import annotations

import logging
from typing import Any

from ..models import Job
from .base import Source

log = logging.getLogger("jobhunter.sources.jobspy")


class JobSpySource(Source):
    name = "jobspy"

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        cfg = matrix.get("jobspy") or {}
        if not cfg.get("enabled", False):
            return []
        try:
            from jobspy import scrape_jobs  # type: ignore
        except ImportError:
            log.info("python-jobspy not installed — skipping LinkedIn/Indeed/Glassdoor. "
                     "Install with: pip install python-jobspy")
            return []

        sites = cfg.get("sites", ["linkedin", "indeed"])
        terms = matrix.get("keyword_seeds", [])[: int(cfg.get("max_terms", 6))]
        locations = cfg.get("locations", ["India"])
        # ZipRecruiter/Naukri/Glassdoor block datacentre IPs (403 / recaptcha). Routing
        # through residential proxies makes those sites usable; without them JobSpy
        # simply returns nothing for those sites and the run carries on.
        proxies = cfg.get("proxies") or None
        out: list[Job] = []

        for term in terms:
            for location in locations:
                try:
                    df = scrape_jobs(
                        site_name=sites,
                        search_term=term,
                        location=location,
                        results_wanted=int(cfg.get("results_wanted", 20)),
                        hours_old=int(cfg.get("hours_old", 72)),
                        country_indeed=cfg.get("country_indeed", "India"),
                        is_remote=bool(cfg.get("is_remote", True)),
                        linkedin_fetch_description=True,
                        proxies=proxies,
                        verbose=0,
                    )
                except Exception as exc:  # scraper blocks / captchas are expected
                    log.info("jobspy '%s' @ '%s' failed: %s", term, location, exc)
                    continue
                if df is None or getattr(df, "empty", True):
                    continue
                for row in df.to_dict("records"):
                    url = str(row.get("job_url") or row.get("job_url_direct") or "")
                    title = str(row.get("title") or "")
                    if not url or not title:
                        continue
                    out.append(Job(
                        source=f"{row.get('site', 'jobspy')}",
                        title=title,
                        company=str(row.get("company") or ""),
                        url=url,
                        description=str(row.get("description") or ""),
                        location=str(row.get("location") or location),
                        work_mode="remote" if row.get("is_remote") else "unknown",
                        salary=_salary(row),
                        posted_at=str(row.get("date_posted") or ""),
                        employment_type=str(row.get("job_type") or ""),
                    ))
        return out


def _salary(row: dict[str, Any]) -> str:
    lo, hi, cur = row.get("min_amount"), row.get("max_amount"), row.get("currency") or ""
    if lo or hi:
        return f"{lo or ''}-{hi or ''} {cur} {row.get('interval') or ''}".strip()
    return ""
