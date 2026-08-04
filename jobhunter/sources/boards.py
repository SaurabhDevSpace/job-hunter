"""Public remote-job boards with open (key-free) APIs / feeds.

RemoteOK, Remotive, Arbeitnow, Jobicy, Himalayas, WeWorkRemotely (RSS).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..models import Job
from .base import Source, get_json, http_get, matches_any


def _seeds(matrix: dict[str, Any]) -> list[str]:
    return matrix.get("keyword_seeds", []) or []


class RemoteOKSource(Source):
    name = "remoteok"
    API = "https://remoteok.com/api"

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        data = get_json(self.API) or []
        seeds = _seeds(matrix)
        out: list[Job] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue
            blob = f"{item.get('position','')} {' '.join(item.get('tags') or [])} {item.get('description','')[:1500]}"
            if not matches_any(blob, seeds):
                continue
            out.append(Job(
                source=self.name,
                title=item.get("position", ""),
                company=item.get("company", ""),
                url=item.get("url") or item.get("apply_url") or "",
                description=item.get("description", ""),
                location=item.get("location") or "Remote",
                work_mode="remote",
                salary=str(item.get("salary_min") or "") and
                       f"{item.get('salary_min')}-{item.get('salary_max')} USD",
                posted_at=item.get("date", ""),
                department=", ".join(item.get("tags") or []),
            ))
        return out


class RemotiveSource(Source):
    name = "remotive"
    API = "https://remotive.com/api/remote-jobs"

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        out: list[Job] = []
        seen: set[str] = set()
        for seed in _seeds(matrix)[:12]:
            data = get_json(self.API, params={"search": seed, "limit": 40}) or {}
            for item in data.get("jobs", []) or []:
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                out.append(Job(
                    source=self.name,
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    url=url,
                    description=item.get("description", ""),
                    location=item.get("candidate_required_location") or "Remote",
                    work_mode="remote",
                    salary=item.get("salary", "") or "",
                    posted_at=item.get("publication_date", ""),
                    department=item.get("category", ""),
                    employment_type=item.get("job_type", ""),
                ))
        return out


class ArbeitnowSource(Source):
    name = "arbeitnow"
    API = "https://arbeitnow.com/api/job-board-api"

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        for page in range(1, 6):
            data = get_json(self.API, params={"page": page}) or {}
            rows = data.get("data") or []
            if not rows:
                break
            for item in rows:
                blob = f"{item.get('title','')} {' '.join(item.get('tags') or [])}"
                if not matches_any(blob, seeds):
                    continue
                out.append(Job(
                    source=self.name,
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    url=item.get("url", ""),
                    description=item.get("description", ""),
                    location=item.get("location", ""),
                    work_mode="remote" if item.get("remote") else "unknown",
                    posted_at=str(item.get("created_at", "")),
                    department=", ".join(item.get("tags") or []),
                    employment_type=", ".join(item.get("job_types") or []),
                ))
        return out


class JobicySource(Source):
    name = "jobicy"
    API = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        out: list[Job] = []
        seen: set[str] = set()
        for tag in ["business", "operations", "management", "supply-chain", "finance", "data"]:
            data = get_json(self.API, params={"count": 50, "tag": tag}) or {}
            for item in data.get("jobs", []) or []:
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                if not matches_any(item.get("jobTitle", ""), _seeds(matrix)):
                    continue
                seen.add(url)
                out.append(Job(
                    source=self.name,
                    title=item.get("jobTitle", ""),
                    company=item.get("companyName", ""),
                    url=url,
                    description=item.get("jobDescription", "") or item.get("jobExcerpt", ""),
                    location=item.get("jobGeo", "") or "Remote",
                    work_mode="remote",
                    salary=_jobicy_salary(item),
                    posted_at=item.get("pubDate", ""),
                    department=", ".join(item.get("jobIndustry") or []),
                    employment_type=", ".join(item.get("jobType") or []),
                ))
        return out


def _jobicy_salary(item: dict[str, Any]) -> str:
    lo, hi, cur = item.get("annualSalaryMin"), item.get("annualSalaryMax"), item.get("salaryCurrency", "")
    return f"{lo}-{hi} {cur}".strip() if lo or hi else ""


class HimalayasSource(Source):
    name = "himalayas"
    API = "https://himalayas.app/jobs/api"

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        for offset in (0, 100, 200):
            data = get_json(self.API, params={"limit": 100, "offset": offset}) or {}
            rows = data.get("jobs") or []
            if not rows:
                break
            for item in rows:
                if not matches_any(item.get("title", ""), seeds):
                    continue
                out.append(Job(
                    source=self.name,
                    title=item.get("title", ""),
                    company=item.get("companyName", ""),
                    url=item.get("applicationLink") or item.get("guid", ""),
                    description=item.get("description", "") or item.get("excerpt", ""),
                    location=", ".join(item.get("locationRestrictions") or []) or "Remote",
                    work_mode="remote",
                    salary=str(item.get("salary") or ""),
                    posted_at=str(item.get("pubDate", "")),
                    department=", ".join(item.get("categories") or []),
                    employment_type=", ".join(item.get("employmentType") or []),
                ))
        return out


class WeWorkRemotelySource(Source):
    name = "weworkremotely"
    FEEDS = [
        "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
        "https://weworkremotely.com/categories/remote-business-jobs.rss",
        "https://weworkremotely.com/categories/remote-product-jobs.rss",
        "https://weworkremotely.com/remote-jobs.rss",
    ]

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        seeds = _seeds(matrix)
        out: list[Job] = []
        seen: set[str] = set()
        for feed in self.FEEDS:
            r = http_get(feed, headers={"Accept": "application/rss+xml, application/xml"})
            if not r:
                continue
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                continue
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not link or link in seen or not matches_any(title, seeds):
                    continue
                seen.add(link)
                company, role = _split_wwr_title(title)
                out.append(Job(
                    source=self.name,
                    title=role,
                    company=company,
                    url=link,
                    description=item.findtext("description") or "",
                    location=(item.findtext("region") or "Remote").strip(),
                    work_mode="remote",
                    posted_at=(item.findtext("pubDate") or "").strip(),
                    department=(item.findtext("category") or "").strip(),
                ))
        return out


def _split_wwr_title(title: str) -> tuple[str, str]:
    m = re.match(r"^\s*(.+?)\s*[:\u2013\-]\s*(.+)$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", title.strip()


PUBLIC_SOURCES: list[type[Source]] = [
    RemoteOKSource, RemotiveSource, ArbeitnowSource,
    JobicySource, HimalayasSource, WeWorkRemotelySource,
]
