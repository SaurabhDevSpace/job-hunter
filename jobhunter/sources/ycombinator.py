"""Y Combinator startup jobs (Work at a Startup).

YC's job board is server-rendered by Inertia.js: the full listing payload is embedded
in a `data-page` attribute as JSON, so no private API key or login is needed. We read
the Operations/Finance/Product role pages, which is where this candidate's roles live.

These postings are high-value: well-paid startup roles with far less competition than
the mainstream aggregators, and many never syndicate to LinkedIn or Indeed.
"""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from ..models import Job
from .base import Source, http_get, matches_any

log = logging.getLogger("jobhunter.sources.yc")

BASE = "https://www.ycombinator.com"
_DATA_PAGE = re.compile(r'data-page="(.*?)"(?:\s|>)', re.S)
_REMOTE = re.compile(r"\bremote\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)


class YCombinatorSource(Source):
    """Y Combinator / Work at a Startup listings."""

    name = "ycombinator"
    hidden = True  # startup roles that rarely reach the mainstream aggregators

    def fetch(self, matrix: dict[str, Any]) -> list[Job]:
        cfg = matrix.get("ycombinator") or {}
        if not cfg.get("enabled", True):
            return []

        roles = cfg.get("roles") or ["operations", "finance", "product"]
        seeds = matrix.get("keyword_seeds", [])
        out: list[Job] = []
        seen: set[str] = set()

        for role in roles:
            for posting in self._role_page(str(role)):
                job = self._to_job(posting, role)
                if job is None or job.url in seen:
                    continue
                # 'operations' pages are already on-target; other role pages are only
                # worth keeping when the title actually matches the search matrix.
                if role != "operations" and seeds and not matches_any(job.title, seeds):
                    continue
                seen.add(job.url)
                out.append(job)
        return out

    # ---------------------------------------------------------------- internals
    def _role_page(self, role: str) -> list[dict[str, Any]]:
        r = http_get(f"{BASE}/jobs/role/{role}",
                     headers={"Accept": "text/html,application/xhtml+xml"})
        if not r:
            return []
        m = _DATA_PAGE.search(r.text)
        if not m:
            log.debug("yc: no data-page payload for role %s", role)
            return []
        try:
            data = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError:
            log.debug("yc: unparseable data-page payload for role %s", role)
            return []
        postings = (data.get("props") or {}).get("jobPostings") or []
        return [p for p in postings if isinstance(p, dict)]

    @staticmethod
    def _to_job(p: dict[str, Any], role: str) -> Job | None:
        title = str(p.get("title") or "").strip()
        url = str(p.get("url") or "").strip()
        if not title or not url:
            return None
        if url.startswith("/"):
            url = BASE + url

        location = str(p.get("location") or "").strip()
        mode = "remote" if _REMOTE.search(location) else (
            "hybrid" if _HYBRID.search(location) else "onsite")

        skills = p.get("skills")
        skills_txt = ", ".join(str(s) for s in skills) if isinstance(skills, list) else ""
        description = " ".join(x for x in [
            str(p.get("companyOneLiner") or ""),
            f"Role: {p.get('prettyRole') or role}." if p.get("prettyRole") or role else "",
            f"Experience: {p.get('minExperience')}." if p.get("minExperience") else "",
            f"Equity: {p.get('equityRange')}." if p.get("equityRange") else "",
            f"Visa: {p.get('visa')}." if p.get("visa") else "",
            f"Skills: {skills_txt}." if skills_txt else "",
            f"YC batch: {p.get('companyBatchName')}." if p.get("companyBatchName") else "",
        ] if x).strip()

        return Job(
            source="ycombinator",
            title=title,
            company=str(p.get("companyName") or "").strip(),
            url=url,
            description=description,
            location=location,
            work_mode=mode,
            salary=str(p.get("salaryRange") or ""),
            posted_at=str(p.get("createdAt") or ""),
            employment_type=str(p.get("type") or ""),
            department=str(p.get("prettyRole") or role),
        )
