"""API budget guard + persistent response cache.

Free OpenRouter keys are hard-capped (typically 50 requests/day, 20/minute), so every
call must earn its place. This module provides:

* `BudgetGuard`  — a persistent daily/per-run request ledger with reserved allocations
                   per task, so resume generation can never be starved by JD parsing.
* `ResponseCache`— content-addressed cache of prompt -> completion. Re-runs, re-scores
                   and dashboard retries are then free.
* Auto-detection of the key's real limits via GET /api/v1/key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("jobhunter.llm.budget")

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_ledger (
    day        TEXT NOT NULL,
    task       TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, task)
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key        TEXT PRIMARY KEY,
    task       TEXT,
    model      TEXT,
    response   TEXT NOT NULL,
    created_at TEXT,
    hits       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS llm_key_info (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    payload    TEXT,
    checked_at REAL
);
CREATE TABLE IF NOT EXISTS model_reliability (
    model      TEXT NOT NULL,
    task       TEXT NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 0,
    bad_json   INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (model, task)
);
"""


class BudgetExhausted(RuntimeError):
    """Raised when the request budget for the day/run/task is used up."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class BudgetState:
    day: str
    used_today: int
    daily_limit: int
    used_this_run: int
    run_limit: int
    per_task: dict[str, int]
    reserved: dict[str, int]
    source: str = "policy"

    @property
    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self.used_today)

    @property
    def remaining_run(self) -> int:
        return max(0, self.run_limit - self.used_this_run)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "remaining_today": self.remaining_today,
                "remaining_run": self.remaining_run}


class BudgetGuard:
    def __init__(self, db_path: Path | str, policy: dict[str, Any], api_key: str = ""):
        self.path = str(db_path)
        self.policy = policy or {}
        self.api_key = api_key
        self._lock = threading.Lock()
        self._run_used = 0
        self._minute_stamps: list[float] = []
        self._detected: dict[str, Any] | None = None
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        cx = sqlite3.connect(self.path, timeout=30)
        cx.row_factory = sqlite3.Row
        return _Ctx(cx)

    # -------------------------------------------------------------- limits
    @property
    def daily_limit(self) -> int:
        env = os.getenv("JOBHUNTER_MAX_LLM_CALLS_PER_DAY", "").strip()
        if env.isdigit():
            return int(env)
        detected = self.detect_limits().get("daily_request_limit")
        policy = int(self.policy.get("max_calls_per_day", 45))
        if detected:
            margin = float(self.policy.get("safety_margin", 0.9))
            return max(1, min(policy, int(detected * margin)))
        return policy

    @property
    def run_limit(self) -> int:
        env = os.getenv("JOBHUNTER_MAX_LLM_CALLS_PER_RUN", "").strip()
        if env.isdigit():
            return int(env)
        return int(self.policy.get("max_calls_per_run", 40))

    @property
    def rpm_limit(self) -> int:
        return int(self.policy.get("max_calls_per_minute", 18))

    def detect_limits(self) -> dict[str, Any]:
        """Ask OpenRouter what this key may actually do (cached for 6h)."""
        if self._detected is not None:
            return self._detected
        cached = self._read_key_info()
        if cached is not None:
            self._detected = cached
            return cached
        info: dict[str, Any] = {}
        if self.api_key:
            try:
                r = requests.get("https://openrouter.ai/api/v1/key", timeout=15,
                                 headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code == 200:
                    d = (r.json() or {}).get("data") or {}
                    free_tier = bool(d.get("is_free_tier", True))
                    # OpenRouter policy: free-tier keys get 50 free-model requests/day;
                    # keys with >=10 credits purchased get 1000/day.
                    info = {
                        "is_free_tier": free_tier,
                        "daily_request_limit": int(self.policy.get(
                            "free_tier_daily_requests", 50)) if free_tier else int(
                            self.policy.get("paid_tier_daily_requests", 1000)),
                        "credit_limit": d.get("limit"),
                        "usage": d.get("usage"),
                        "usage_daily": d.get("usage_daily"),
                    }
            except requests.RequestException as exc:
                log.debug("key limit detection failed: %s", exc)
        self._detected = info
        self._write_key_info(info)
        return info

    def _read_key_info(self) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT payload, checked_at FROM llm_key_info WHERE id=1").fetchone()
        if not row:
            return None
        if time.time() - float(row["checked_at"] or 0) > 6 * 3600:
            return None
        try:
            return json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            return None

    def _write_key_info(self, info: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO llm_key_info (id, payload, checked_at) VALUES (1,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
                      "checked_at=excluded.checked_at", (json.dumps(info), time.time()))

    # --------------------------------------------------------------- ledger
    def used_today(self) -> tuple[int, dict[str, int]]:
        with self._conn() as c:
            rows = c.execute("SELECT task, calls FROM llm_ledger WHERE day=?", (_today(),)).fetchall()
        per_task = {r["task"]: int(r["calls"]) for r in rows}
        return sum(per_task.values()), per_task

    def state(self) -> BudgetState:
        total, per_task = self.used_today()
        return BudgetState(
            day=_today(), used_today=total, daily_limit=self.daily_limit,
            used_this_run=self._run_used, run_limit=self.run_limit,
            per_task=per_task, reserved=dict(self.policy.get("reserve_per_task") or {}),
            source="env/detected/policy",
        )

    def reset_run(self) -> None:
        self._run_used = 0

    def can_spend(self, task: str, n: int = 1) -> tuple[bool, str]:
        st = self.state()
        if st.remaining_today < n:
            return False, (f"daily budget exhausted ({st.used_today}/{st.daily_limit} calls used). "
                           f"Deterministic scoring and offline tailoring remain available.")
        if st.remaining_run < n:
            return False, f"per-run budget exhausted ({st.used_this_run}/{st.run_limit})"
        # Protect reservations belonging to *other* tasks.
        reserved_elsewhere = sum(
            max(0, cnt - st.per_task.get(other, 0))
            for other, cnt in (self.policy.get("reserve_per_task") or {}).items()
            if other != task)
        if st.remaining_today - n < reserved_elsewhere:
            return False, (f"remaining {st.remaining_today} calls are reserved for "
                           f"higher-priority tasks ({reserved_elsewhere} held back)")
        return True, ""

    def spend(self, task: str, n: int = 1) -> None:
        with self._lock:
            self._run_used += n
        with self._conn() as c:
            c.execute("INSERT INTO llm_ledger (day, task, calls) VALUES (?,?,?) "
                      "ON CONFLICT(day, task) DO UPDATE SET calls = calls + ?",
                      (_today(), task, n, n))

    def throttle_rpm(self) -> None:
        """Client-side sliding-window RPM guard (OpenRouter free tier ~20/min)."""
        limit = self.rpm_limit
        if limit <= 0:
            return
        with self._lock:
            now = time.monotonic()
            self._minute_stamps = [t for t in self._minute_stamps if now - t < 60]
            if len(self._minute_stamps) >= limit:
                wait = 60 - (now - self._minute_stamps[0]) + 0.5
                if wait > 0:
                    log.info("client-side RPM guard: sleeping %.1fs", wait)
                    time.sleep(wait)
                    now = time.monotonic()
                    self._minute_stamps = [t for t in self._minute_stamps if now - t < 60]
            self._minute_stamps.append(now)

    def budget_for(self, task: str, wanted: int) -> int:
        """How many calls a stage may plan for, given today's remaining allowance."""
        st = self.state()
        share = self.policy.get("stage_share", {}).get(task)
        cap = min(st.remaining_today, st.remaining_run)
        if share:
            cap = min(cap, max(1, int(st.daily_limit * float(share))))
        return max(0, min(wanted, cap))


class ReliabilityLedger:
    """Persistent per-(model, task) outcome history.

    Free models vary wildly in whether they can actually emit JSON. Remembering this
    across processes stops every run from re-discovering the same failure — which on a
    50-request/day key is a meaningful saving.
    """

    def __init__(self, db_path: Path | str, min_samples: int = 2, bad_ratio: float = 0.5):
        self.path = str(db_path)
        self.min_samples = min_samples
        self.bad_ratio = bad_ratio
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with _Ctx(sqlite3.connect(self.path, timeout=30)) as c:
            c.executescript(SCHEMA)

    def record(self, model: str, task: str, *, ok: bool = False,
               bad_json: bool = False, failed: bool = False) -> None:
        now = _now_iso()
        with _Ctx(sqlite3.connect(self.path, timeout=30)) as c:
            c.execute(
                "INSERT INTO model_reliability (model, task, ok, bad_json, failed, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(model, task) DO UPDATE SET "
                "ok = ok + ?, bad_json = bad_json + ?, failed = failed + ?, updated_at = ?",
                (model, task, int(ok), int(bad_json), int(failed), now,
                 int(ok), int(bad_json), int(failed), now))

    def unreliable(self, task: str) -> set[str]:
        """Models that have repeatedly refused to produce parseable JSON for this task."""
        cx = sqlite3.connect(self.path, timeout=30)
        cx.row_factory = sqlite3.Row
        with _Ctx(cx) as c:
            rows = c.execute("SELECT model, ok, bad_json FROM model_reliability "
                             "WHERE task=?", (task,)).fetchall()
        out: set[str] = set()
        for r in rows:
            total = r["ok"] + r["bad_json"]
            if total >= self.min_samples and r["bad_json"] / total >= self.bad_ratio:
                out.add(r["model"])
        return out

    def report(self) -> list[dict[str, Any]]:
        cx = sqlite3.connect(self.path, timeout=30)
        cx.row_factory = sqlite3.Row
        with _Ctx(cx) as c:
            rows = c.execute("SELECT * FROM model_reliability "
                             "ORDER BY task, bad_json DESC, ok DESC").fetchall()
        return [dict(r) for r in rows]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResponseCache:
    """Content-addressed LLM response cache — makes repeats and retries free."""

    def __init__(self, db_path: Path | str, ttl_hours: float = 336.0, enabled: bool = True):
        self.path = str(db_path)
        self.ttl = ttl_hours * 3600
        self.enabled = enabled
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with _Ctx(sqlite3.connect(self.path, timeout=30)) as c:
            c.executescript(SCHEMA)

    @staticmethod
    def key_for(task: str, messages: list[dict[str, str]], params: dict[str, Any]) -> str:
        blob = json.dumps({"task": task, "messages": messages,
                           "t": params.get("temperature"), "m": params.get("max_tokens")},
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> tuple[str, str] | None:
        if not self.enabled:
            return None
        cx = sqlite3.connect(self.path, timeout=30)
        cx.row_factory = sqlite3.Row
        with _Ctx(cx) as c:
            row = c.execute("SELECT response, model, created_at FROM llm_cache WHERE key=?",
                            (key,)).fetchone()
            if not row:
                return None
            try:
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(row["created_at"])).total_seconds()
            except (TypeError, ValueError):
                age = 0
            if self.ttl and age > self.ttl:
                c.execute("DELETE FROM llm_cache WHERE key=?", (key,))
                return None
            c.execute("UPDATE llm_cache SET hits = hits + 1 WHERE key=?", (key,))
            return row["response"], row["model"] or ""

    def put(self, key: str, task: str, model: str, response: str) -> None:
        if not self.enabled or not response.strip():
            return
        with _Ctx(sqlite3.connect(self.path, timeout=30)) as c:
            c.execute("INSERT INTO llm_cache (key, task, model, response, created_at, hits) "
                      "VALUES (?,?,?,?,?,0) ON CONFLICT(key) DO UPDATE SET "
                      "response=excluded.response, model=excluded.model, "
                      "created_at=excluded.created_at",
                      (key, task, model, response,
                       datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def stats(self) -> dict[str, Any]:
        cx = sqlite3.connect(self.path, timeout=30)
        cx.row_factory = sqlite3.Row
        with _Ctx(cx) as c:
            row = c.execute("SELECT COUNT(*) n, COALESCE(SUM(hits),0) hits FROM llm_cache").fetchone()
        return {"entries": row["n"], "hits_served": row["hits"]}


class _Ctx:
    """Tiny commit/close context manager for sqlite connections."""

    def __init__(self, cx: sqlite3.Connection):
        self.cx = cx

    def __enter__(self) -> sqlite3.Connection:
        return self.cx

    def __exit__(self, *exc: Any) -> None:
        try:
            self.cx.commit()
        finally:
            self.cx.close()
