"""SQLite persistence layer (jobs, scores, artifacts, run log)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import DB_PATH, db_path, ensure_dirs
from .models import Job, JobScore

CUSTOM_TAILOR_SOURCE = "My Own Search"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    dedupe_key      TEXT NOT NULL,
    source          TEXT NOT NULL,
    is_hidden       INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    url             TEXT NOT NULL,
    location        TEXT,
    work_mode       TEXT,
    salary          TEXT,
    department      TEXT,
    employment_type TEXT,
    posted_at       TEXT,
    description     TEXT,
    fetched_at      TEXT,
    first_seen      TEXT,
    last_seen       TEXT,
    status          TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS scores (
    job_id               TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    match_score          REAL NOT NULL,
    tier                 TEXT,
    breakdown            TEXT,
    matched_competencies TEXT,
    missing_keywords     TEXT,
    ats_keywords         TEXT,
    seniority_detected   TEXT,
    growth_stage         TEXT,
    title_family         TEXT,
    llm_score            REAL,
    llm_rationale        TEXT,
    llm_model            TEXT,
    scored_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_total ON scores(match_score DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,          -- resume_docx | resume_pdf | resume_html
                                          -- | resume_md | resume_json | cover_letter | ats_txt
    path          TEXT NOT NULL,
    model_used    TEXT,
    ats_score     REAL,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT,
    finished_at   TEXT,
    sources_ok    TEXT,
    sources_failed TEXT,
    jobs_found    INTEGER DEFAULT 0,
    jobs_new      INTEGER DEFAULT 0,
    jobs_scored   INTEGER DEFAULT 0,
    resumes_built INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'running',
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT,
    model       TEXT,
    ok          INTEGER,
    latency_ms  INTEGER,
    error       TEXT,
    created_at  TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_IST = timezone(timedelta(hours=5, minutes=30))  # India Standard Time for display


def _fmt_ts(iso: str | None) -> str:
    """Render an ISO timestamp as 'DD Mon YYYY, HH:MM IST' for display.

    Returns '' when the value is missing/unparseable so the template can hide it.
    """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_IST).strftime("%d %b %Y, %H:%M") + " IST"
    except (ValueError, TypeError):
        return ""


class Store:
    def __init__(self, path: Path | str | None = None):
        ensure_dirs()
        self.path = str(path or db_path())
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(self.path, timeout=30)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA foreign_keys=ON")
        try:
            yield cx
            cx.commit()
        finally:
            cx.close()

    # ------------------------------------------------------------------ jobs
    def upsert_jobs(self, jobs: Iterable[Job]) -> tuple[int, int]:
        """Returns (total_seen, newly_inserted).

        Identity is the cross-source `dedupe_key` (company + normalized title), not the
        URL — the same role listed on a company ATS and an aggregator, or posted under
        several location-specific URLs, must occupy exactly one row. The first-seen
        job_id is kept stable so existing scores and artifacts stay attached, while the
        stored content is upgraded whenever a richer version of the posting appears.
        """
        total = new = 0
        with self.conn() as c:
            for job in jobs:
                total += 1
                d = job.to_dict()
                row = c.execute(
                    "SELECT job_id, source, is_hidden, description, work_mode, salary "
                    "FROM jobs WHERE job_id=? OR dedupe_key=? "
                    "ORDER BY (job_id=?) DESC, first_seen ASC LIMIT 1",
                    (d["job_id"], d["dedupe_key"], d["job_id"]),
                ).fetchone()

                if row:
                    incoming = (int(d["is_hidden"]), len(d["description"] or ""))
                    existing = (int(row["is_hidden"] or 0), len(row["description"] or ""))
                    if incoming > existing:
                        # Richer version wins: prefer the company ATS listing / fuller JD.
                        c.execute(
                            "UPDATE jobs SET source=?, is_hidden=?, url=?, description=?, "
                            "location=COALESCE(NULLIF(?,''),location), "
                            "work_mode=CASE WHEN ?='unknown' THEN work_mode ELSE ? END, "
                            "salary=COALESCE(NULLIF(?,''),salary), last_seen=? WHERE job_id=?",
                            (d["source"], int(d["is_hidden"]), d["url"], d["description"],
                             d["location"], d["work_mode"], d["work_mode"], d["salary"],
                             _now(), row["job_id"]),
                        )
                    else:
                        c.execute(
                            "UPDATE jobs SET last_seen=?, "
                            "description=COALESCE(NULLIF(?,''),description) WHERE job_id=?",
                            (_now(), d["description"], row["job_id"]),
                        )
                    continue

                new += 1
                c.execute(
                    """INSERT INTO jobs (job_id, dedupe_key, source, is_hidden, title, company, url,
                                         location, work_mode, salary, department, employment_type,
                                         posted_at, description, fetched_at, first_seen, last_seen, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
                    (d["job_id"], d["dedupe_key"], d["source"], int(d["is_hidden"]), d["title"],
                     d["company"], d["url"], d["location"], d["work_mode"], d["salary"],
                     d["department"], d["employment_type"], d["posted_at"], d["description"],
                     d["fetched_at"], _now(), _now()),
                )
        return total, new

    def collapse_duplicates(self) -> int:
        """Merge pre-existing rows that share a dedupe_key. Returns rows removed.

        Survivor = the row that already carries a score, else the hidden-market/richest
        one. Scores and artifacts are re-pointed at the survivor so nothing is lost.
        """
        removed = 0
        with self.conn() as c:
            dupes = c.execute(
                "SELECT dedupe_key FROM jobs GROUP BY dedupe_key HAVING COUNT(*) > 1"
            ).fetchall()
            for (key,) in [(r["dedupe_key"],) for r in dupes]:
                rows = c.execute(
                    "SELECT j.job_id, j.is_hidden, LENGTH(COALESCE(j.description,'')) dlen, "
                    "       (s.job_id IS NOT NULL) scored "
                    "FROM jobs j LEFT JOIN scores s USING(job_id) WHERE j.dedupe_key=? "
                    "ORDER BY scored DESC, j.is_hidden DESC, dlen DESC, j.first_seen ASC",
                    (key,),
                ).fetchall()
                survivor, losers = rows[0]["job_id"], [r["job_id"] for r in rows[1:]]
                for loser in losers:
                    c.execute("UPDATE artifacts SET job_id=? WHERE job_id=?", (survivor, loser))
                    c.execute("DELETE FROM scores WHERE job_id=?", (loser,))
                    c.execute("DELETE FROM jobs WHERE job_id=?", (loser,))
                    removed += 1
        return removed

    def canonical_map(self, dedupe_keys: Iterable[str]) -> dict[str, str]:
        """dedupe_key -> the job_id actually stored for it.

        After a merge the in-memory Job may carry a job_id that was never inserted
        (an equivalent posting already owned the row), so callers must resolve the
        stored id before writing scores, artifacts or status.
        """
        keys = list({k for k in dedupe_keys if k})
        out: dict[str, str] = {}
        with self.conn() as c:
            for i in range(0, len(keys), 400):
                chunk = keys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                for r in c.execute(
                        f"SELECT dedupe_key, job_id FROM jobs WHERE dedupe_key IN ({marks})",
                        chunk):
                    out[r["dedupe_key"]] = r["job_id"]
        return out

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute(
                "SELECT j.*, s.match_score, s.tier, s.breakdown, s.matched_competencies, "
                "s.missing_keywords, s.ats_keywords, s.seniority_detected, s.growth_stage, s.title_family, "
                "s.llm_score, s.llm_rationale, s.llm_model "
                "FROM jobs j LEFT JOIN scores s USING(job_id) WHERE j.job_id=?", (job_id,)
            ).fetchone()
        return _hydrate(row) if row else None

    def unscored_jobs(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT j.* FROM jobs j LEFT JOIN scores s USING(job_id) "
                "WHERE s.job_id IS NULL LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def list_jobs(self, min_score: float = 0, tier: str | None = None, work_mode: str | None = None,
                  hidden_only: bool = False, source: str | None = None, limit: int = 200,
                  query: str | None = None) -> list[dict[str, Any]]:
        sql = ["SELECT j.*, s.match_score, s.tier, s.breakdown, s.matched_competencies,",
               "s.missing_keywords, s.ats_keywords, s.seniority_detected, s.growth_stage,",
               "s.title_family,",
               "s.llm_score, s.llm_rationale, s.llm_model",
               "FROM jobs j LEFT JOIN scores s USING(job_id) WHERE 1=1"]
        args: list[Any] = []
        if min_score:
            # Always include custom-tailor rows so manually added requirements stay visible
            # even when score filters are active.
            sql.append("AND (j.source = ? OR COALESCE(s.match_score,0) >= ?)")
            args += [CUSTOM_TAILOR_SOURCE, min_score]
        if tier:
            sql.append("AND s.tier = ?"); args.append(tier)
        if work_mode:
            sql.append("AND j.work_mode = ?"); args.append(work_mode)
        if hidden_only:
            sql.append("AND j.is_hidden = 1")
        if source:
            sql.append("AND j.source = ?"); args.append(source)
        if query:
            sql.append("AND (j.title LIKE ? OR j.company LIKE ?)")
            args += [f"%{query}%", f"%{query}%"]
        sql.append("ORDER BY COALESCE(s.match_score,0) DESC, j.first_seen DESC LIMIT ?")
        args.append(limit)
        with self.conn() as c:
            rows = c.execute(" ".join(sql), args).fetchall()
        return [_hydrate(r) for r in rows]

    def set_status(self, job_id: str, status: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, job_id))

    # ---------------------------------------------------------------- scores
    def save_score(self, score: JobScore) -> None:
        d = score.to_dict()
        with self.conn() as c:
            c.execute(
                """INSERT INTO scores (job_id, match_score, tier, breakdown, matched_competencies,
                                       missing_keywords, ats_keywords, seniority_detected,
                                       growth_stage, title_family, llm_score, llm_rationale,
                                       llm_model, scored_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     match_score=excluded.match_score, tier=excluded.tier,
                     breakdown=excluded.breakdown, matched_competencies=excluded.matched_competencies,
                     missing_keywords=excluded.missing_keywords, ats_keywords=excluded.ats_keywords,
                     seniority_detected=excluded.seniority_detected, growth_stage=excluded.growth_stage,
                     title_family=excluded.title_family,
                     llm_score=excluded.llm_score, llm_rationale=excluded.llm_rationale,
                     llm_model=excluded.llm_model, scored_at=excluded.scored_at""",
                (d["job_id"], d["match_score"], d["tier"], json.dumps(d["breakdown"]),
                 json.dumps(d["matched_competencies"]), json.dumps(d["missing_keywords"]),
                 json.dumps(d["ats_keywords"]), d["seniority_detected"], d["growth_stage"],
                 d["title_family"], d["llm_score"], d["llm_rationale"], d["llm_model"], d["scored_at"]),
            )

    # ------------------------------------------------------------- artifacts
    def add_artifact(self, job_id: str, kind: str, path: str, model_used: str = "",
                     ats_score: float | None = None) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO artifacts (job_id, kind, path, model_used, ats_score, created_at) "
                      "VALUES (?,?,?,?,?,?)", (job_id, kind, path, model_used, ats_score, _now()))

    def artifacts(self, job_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self.conn() as c:
            if job_id:
                rows = c.execute("SELECT a.*, j.title, j.company FROM artifacts a "
                                 "LEFT JOIN jobs j USING(job_id) WHERE a.job_id=? "
                                 "ORDER BY a.id DESC", (job_id,)).fetchall()
            else:
                rows = c.execute("SELECT a.*, j.title, j.company FROM artifacts a "
                                 "LEFT JOIN jobs j USING(job_id) ORDER BY a.id DESC LIMIT ?",
                                 (limit,)).fetchall()
        out = [dict(r) for r in rows]
        for a in out:
            a["created_at_disp"] = _fmt_ts(a.get("created_at"))
        return out

    # ------------------------------------------------------------------ runs
    def start_run(self) -> int:
        with self.conn() as c:
            # A previous process killed mid-run would otherwise leave 'running' rows
            # forever, making the dashboard look permanently busy.
            c.execute("UPDATE runs SET status='interrupted', finished_at=? "
                      "WHERE status='running'", (_now(),))
            cur = c.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (_now(),))
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        fields.setdefault("finished_at", _now())
        fields.setdefault("status", "completed")
        for k in ("sources_ok", "sources_failed"):
            if isinstance(fields.get(k), (list, dict)):
                fields[k] = json.dumps(fields[k])
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), run_id))

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def log_llm(self, task: str, model: str, ok: bool, latency_ms: int, error: str = "") -> None:
        with self.conn() as c:
            c.execute("INSERT INTO llm_calls (task, model, ok, latency_ms, error, created_at) "
                      "VALUES (?,?,?,?,?,?)", (task, model, int(ok), latency_ms, error[:500], _now()))

    def stats(self) -> dict[str, Any]:
        with self.conn() as c:
            g = lambda q, *a: c.execute(q, a).fetchone()[0]  # noqa: E731
            return {
                "total_jobs": g("SELECT COUNT(*) FROM jobs"),
                "hidden_jobs": g("SELECT COUNT(*) FROM jobs WHERE is_hidden=1"),
                "scored": g("SELECT COUNT(*) FROM scores"),
                "tier_a_plus": g("SELECT COUNT(*) FROM scores WHERE tier='A+'"),
                "tier_a": g("SELECT COUNT(*) FROM scores WHERE tier='A'"),
                "tier_b": g("SELECT COUNT(*) FROM scores WHERE tier='B'"),
                "remote": g("SELECT COUNT(*) FROM jobs WHERE work_mode='remote'"),
                "hybrid": g("SELECT COUNT(*) FROM jobs WHERE work_mode='hybrid'"),
                "resumes": g("SELECT COUNT(*) FROM artifacts WHERE kind='resume_docx'"),
                "sources": [dict(r) for r in c.execute(
                    "SELECT source, COUNT(*) n FROM jobs GROUP BY source ORDER BY n DESC").fetchall()],
                "model_usage": [dict(r) for r in c.execute(
                    "SELECT model, SUM(ok) ok, COUNT(*) total FROM llm_calls "
                    "GROUP BY model ORDER BY total DESC").fetchall()],
            }


def _hydrate(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in ("breakdown", "matched_competencies", "missing_keywords", "ats_keywords"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                d[k] = [] if k != "breakdown" else {}
        else:
            d[k] = {} if k == "breakdown" else []
    return d
