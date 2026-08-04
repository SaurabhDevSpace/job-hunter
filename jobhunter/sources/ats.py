"""HIDDEN JOB MARKET connectors — company-owned ATS portals.

These endpoints are the same ones the companies' own career pages call, so they
surface roles that are frequently never syndicated to LinkedIn / Indeed:

  Greenhouse : GET  boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true
  Lever      : GET  api.lever.co/v0/postings/<company>?mode=json
  Ashby      : POST api.ashbyhq.com/posting-api/job-board/<company>
  Workday    : POST <host>/wday/cxs/<tenant>/<site>/jobs
  SmartRecr. : GET  api.smartrecruiters.com/v1/companies/<token>/postings
  Workable   : GET  apply.workable.com/api/v1/widget/accounts/<token>
  Recruitee  : GET  <token>.recruitee.com/api/offers/
"""
from __future__ import annotations

import base64
from typing import Any

from ..models import Job
from .base import Source, get_json, http_post, matches_any


def _seeds(matrix: dict[str, Any]) -> list[str]:
    return matrix.get("keyword_seeds", []) or []


def _ats_cfg(matrix: dict[str, Any], key: str) -> list[Any]:
    return (matrix.get("ats") or {}).get(key) or []


class GreenhouseSource(Source):
    name = "greenhouse"
    hidden = True

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        for token in _ats_cfg(matrix, "greenhouse"):
            data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                            params={"content": "true"})
            if not data:
                continue
            company = (data.get("meta", {}) or {}).get("name") or token.title()
            for item in data.get("jobs", []) or []:
                title = item.get("title", "")
                if not matches_any(title, seeds):
                    continue
                loc = ((item.get("location") or {}).get("name")) or ""
                content = item.get("content") or ""
                if content and "&lt;" in content:  # Greenhouse double-escapes
                    content = content.replace("&lt;", "<").replace("&gt;", ">")
                out.append(Job(
                    source=self.name, title=title, company=company,
                    url=item.get("absolute_url", ""), description=content, location=loc,
                    posted_at=item.get("updated_at", ""),
                    department=", ".join(d.get("name", "") for d in item.get("departments") or []),
                ))
        return out


class LeverSource(Source):
    name = "lever"
    hidden = True

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        for company in _ats_cfg(matrix, "lever"):
            data = get_json(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
            if not isinstance(data, list):
                continue
            for item in data:
                title = item.get("text", "")
                if not matches_any(title, seeds):
                    continue
                cat = item.get("categories") or {}
                desc = item.get("descriptionPlain") or item.get("description") or ""
                for lst in item.get("lists") or []:
                    desc += f"\n\n{lst.get('text','')}\n{lst.get('content','')}"
                out.append(Job(
                    source=self.name, title=title, company=company.title(),
                    url=item.get("hostedUrl") or item.get("applyUrl", ""),
                    description=desc, location=cat.get("location", "") or "",
                    work_mode="remote" if str(cat.get("location", "")).lower().find("remote") >= 0 else "unknown",
                    posted_at=str(item.get("createdAt", "")),
                    department=cat.get("team", "") or cat.get("department", "") or "",
                    employment_type=cat.get("commitment", "") or "",
                ))
        return out


class AshbySource(Source):
    name = "ashby"
    hidden = True
    API = "https://api.ashbyhq.com/posting-api/job-board/{company}"

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        for company in _ats_cfg(matrix, "ashby"):
            data = get_json(self.API.format(company=company),
                            params={"includeCompensation": "true"})
            if not data:
                continue
            for item in data.get("jobs", []) or []:
                title = item.get("title", "")
                if not matches_any(title, seeds):
                    continue
                out.append(Job(
                    source=self.name, title=title, company=company.title(),
                    url=item.get("jobUrl") or item.get("applyUrl", ""),
                    description=item.get("descriptionHtml") or item.get("descriptionPlain", ""),
                    location=item.get("location", "") or "",
                    work_mode="remote" if item.get("isRemote") else "unknown",
                    posted_at=item.get("publishedAt", ""),
                    department=item.get("department", "") or item.get("team", "") or "",
                    employment_type=item.get("employmentType", "") or "",
                    salary=str(item.get("compensationTierSummary") or ""),
                ))
        return out


class WorkdaySource(Source):
    """Workday CXS search API — the undocumented endpoint behind myworkdayjobs.com."""
    name = "workday"
    hidden = True

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        out: list[Job] = []
        seeds = _seeds(matrix)[:8]
        for tenant_cfg in _ats_cfg(matrix, "workday"):
            host = str(tenant_cfg.get("host", "")).rstrip("/")
            tenant, site = tenant_cfg.get("tenant"), tenant_cfg.get("site")
            company = tenant_cfg.get("name") or str(tenant).title()
            if not (host and tenant and site):
                continue
            endpoint = f"{host}/wday/cxs/{tenant}/{site}/jobs"
            for seed in seeds:
                r = http_post(endpoint, {
                    "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": seed,
                }, headers={"Accept": "application/json"})
                if not r:
                    continue
                try:
                    body = r.json()
                except ValueError:
                    continue
                for item in body.get("jobPostings", []) or []:
                    path = item.get("externalPath", "")
                    title = item.get("title", "")
                    if not (path and title):
                        continue
                    out.append(Job(
                        source=self.name, title=title, company=company,
                        url=f"{host}/{site}{path}",
                        description=item.get("bulletFields") and
                                    " | ".join(item["bulletFields"]) or "",
                        location=item.get("locationsText", "") or "",
                        posted_at=item.get("postedOn", "") or "",
                        raw={"path": path, "host": host, "tenant": tenant, "site": site},
                    ))
        return out

    @staticmethod
    def hydrate(job: Job) -> Job:
        """Fetch the full JD for a Workday posting (second API call per job)."""
        raw = job.raw or {}
        host, tenant, site, path = raw.get("host"), raw.get("tenant"), raw.get("site"), raw.get("path")
        if not all([host, tenant, site, path]):
            return job
        data = get_json(f"{host}/wday/cxs/{tenant}/{site}{path}")
        info = (data or {}).get("jobPostingInfo") or {}
        if info.get("jobDescription"):
            job.description = info["jobDescription"]
            job.employment_type = info.get("timeType", "") or job.employment_type
            job.location = info.get("location", "") or job.location
            if info.get("remoteType"):
                job.work_mode = "remote" if "remote" in info["remoteType"].lower() else job.work_mode
        return job.normalize()


def encode_workday_cursor(offset: int) -> str:  # pragma: no cover - helper for future paging
    return base64.b64encode(str(offset).encode()).decode()


class SmartRecruitersSource(Source):
    """SmartRecruiters public posting API (Freshworks, Whatfix, ...)."""

    name = "smartrecruiters"
    hidden = True

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        for token in _ats_cfg(matrix, "smartrecruiters"):
            data = get_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                            params={"limit": 100})
            if not data:
                continue
            for item in data.get("content") or []:
                title = item.get("name", "")
                if not matches_any(title, seeds):
                    continue
                loc = item.get("location") or {}
                where = loc.get("fullLocation") or ", ".join(
                    x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
                mode = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else "unknown")
                company = (item.get("company") or {}).get("name") or token.title()
                job_id = item.get("id") or ""
                # The list payload has no description; the detail call carries the job ad.
                description = ""
                detail = get_json(
                    f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}") \
                    if job_id else None
                if detail:
                    sections = ((detail.get("jobAd") or {}).get("sections") or {})
                    description = "\n\n".join(
                        str((sections.get(k) or {}).get("text") or "")
                        for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
                    ).strip()
                out.append(Job(
                    source=self.name, title=title, company=company,
                    url=f"https://jobs.smartrecruiters.com/{token}/{job_id}",
                    description=description, location=where, work_mode=mode,
                    posted_at=str(item.get("releasedDate") or ""),
                    department=(item.get("department") or {}).get("label", ""),
                    employment_type=(item.get("typeOfEmployment") or {}).get("label", ""),
                ))
        return out


class WorkableSource(Source):
    """Workable public widget API (Trail of Bits, ...)."""

    name = "workable"
    hidden = True

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        for token in _ats_cfg(matrix, "workable"):
            data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{token}")
            if not data:
                continue
            company = data.get("name") or token.title()
            for item in data.get("jobs") or []:
                title = item.get("title", "")
                if not matches_any(title, seeds):
                    continue
                where = ", ".join(x for x in [item.get("city"), item.get("state"),
                                              item.get("country")] if x)
                out.append(Job(
                    source=self.name, title=title, company=company,
                    url=item.get("url") or item.get("shortlink") or "",
                    description=item.get("description") or "",
                    location=where,
                    work_mode="remote" if item.get("telecommuting") else "unknown",
                    posted_at=str(item.get("published_on") or ""),
                    department=str(item.get("department") or ""),
                    employment_type=str(item.get("employment_type") or ""),
                ))
        return out


class RecruiteeSource(Source):
    """Recruitee public offers API (MailerLite, ...)."""

    name = "recruitee"
    hidden = True

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        for token in _ats_cfg(matrix, "recruitee"):
            data = get_json(f"https://{token}.recruitee.com/api/offers/")
            if not data:
                continue
            for item in data.get("offers") or []:
                title = item.get("title", "")
                if not matches_any(title, seeds):
                    continue
                where = ", ".join(x for x in [item.get("city"), item.get("country")] if x)
                mode = "hybrid" if item.get("hybrid") else (
                    "remote" if str(item.get("location", "")).lower().startswith("remote") else "unknown")
                out.append(Job(
                    source=self.name, title=title,
                    company=item.get("company_name") or token.title(),
                    url=item.get("careers_url") or item.get("careers_apply_url") or "",
                    description=item.get("description") or "",
                    location=where or str(item.get("location") or ""), work_mode=mode,
                    posted_at=str(item.get("created_at") or ""),
                    department=str(item.get("department") or ""),
                    employment_type=str(item.get("employment_type_code") or ""),
                ))
        return out


ATS_SOURCES: list[type[Source]] = [GreenhouseSource, LeverSource, AshbySource, WorkdaySource,
                                   SmartRecruitersSource, WorkableSource, RecruiteeSource]
