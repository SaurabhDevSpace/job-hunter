"""Daily automation. Used standalone (`cli schedule`) and embedded in the web app."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .pipeline import Pipeline, RunReport

log = logging.getLogger("jobhunter.scheduler")

_lock = threading.Lock()
LAST_REPORT_PATH = DATA_DIR / "last_run.json"


def daily_job(**kwargs: Any) -> RunReport | None:
    """Run the pipeline once. Guarded so overlapping triggers cannot double-run."""
    if not _lock.acquire(blocking=False):
        log.warning("a run is already in progress — skipping this trigger")
        return None
    try:
        opts = {
            "deep_top_n": int(os.getenv("JOBHUNTER_DEEP_N", 12)),
            "resume_top_n": int(os.getenv("JOBHUNTER_RESUMES", 5)),
            "min_score": float(os.getenv("JOBHUNTER_MIN_SCORE", 60)),
            **kwargs,
        }
        report = Pipeline().run_daily(**opts)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LAST_REPORT_PATH.write_text(
            json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                        **report.to_dict()}, indent=2, default=str), encoding="utf-8")
        return report
    finally:
        _lock.release()


def is_running() -> bool:
    return _lock.locked()


def last_report() -> dict[str, Any] | None:
    if LAST_REPORT_PATH.exists():
        try:
            return json.loads(LAST_REPORT_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def build_scheduler(hour: int = 7, minute: int = 30):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    tz = os.getenv("JOBHUNTER_TZ", "Asia/Kolkata")
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(daily_job, CronTrigger(hour=hour, minute=minute, timezone=tz),
                  id="daily_job_search", replace_existing=True,
                  max_instances=1, misfire_grace_time=3600, coalesce=True)
    log.info("daily job scheduled at %02d:%02d %s", hour, minute, tz)
    return sched


def run_scheduler(hour: int = 7, minute: int = 30, run_now: bool = False) -> None:
    import time

    sched = build_scheduler(hour, minute)
    sched.start()
    if run_now:
        threading.Thread(target=daily_job, daemon=True).start()
    print(f"Scheduler running — daily search at {hour:02d}:{minute:02d}. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()
        print("scheduler stopped")
