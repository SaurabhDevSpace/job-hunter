"""Turso cloud SQLite synchronization layer.

Maintains continuous cloud replication so jobs, scores, runs, and artifacts
persist permanently across Render free-tier container restarts.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import ssl
import urllib.request
from typing import Any
import certifi

log = logging.getLogger("jobhunter.turso")


def get_turso_config() -> tuple[str, str]:
    return os.getenv("TURSO_DATABASE_URL", "").strip(), os.getenv("TURSO_AUTH_TOKEN", "").strip()


def is_turso_enabled() -> bool:
    url, token = get_turso_config()
    return bool(url and token)


def _hrana_pipeline_url(url: str) -> str:
    cleaned = url.replace("libsql://", "https://").rstrip("/")
    if not cleaned.endswith("/v2/pipeline"):
        cleaned += "/v2/pipeline"
    return cleaned


def execute_turso_pipeline(stmts_with_args: list[tuple[str, list[Any] | None]], timeout: int = 15) -> bool:
    url, token = get_turso_config()
    if not (url and token):
        return False
    endpoint = _hrana_pipeline_url(url)
    requests = []
    for sql, args in stmts_with_args:
        req_entry: dict[str, Any] = {"type": "execute", "stmt": {"sql": sql}}
        if args:
            hrana_args = []
            for a in args:
                if a is None:
                    hrana_args.append({"type": "null"})
                elif isinstance(a, bool):
                    hrana_args.append({"type": "integer", "value": "1" if a else "0"})
                elif isinstance(a, int):
                    hrana_args.append({"type": "integer", "value": str(a)})
                elif isinstance(a, float):
                    hrana_args.append({"type": "float", "value": a})
                else:
                    hrana_args.append({"type": "text", "value": str(a)})
            req_entry["stmt"]["args"] = hrana_args
        requests.append(req_entry)

    try:
        body = json.dumps({"requests": requests}).encode()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            results = data.get("results", [])
            for r in results:
                if r.get("type") == "error":
                    log.warning("Turso stmt error: %s", r.get("error"))
            return all(r.get("type") == "ok" for r in results)
    except Exception as exc:
        log.warning("Turso remote execution failed: %s", exc)
        return False


def pull_from_turso(local_conn: sqlite3.Connection, timeout: int = 15) -> int:
    """Pull remote Turso jobs, scores, and artifacts into local SQLite on startup.
    Returns number of records restored. Safe and non-blocking with tight timeout.
    """
    url, token = get_turso_config()
    if not (url and token):
        return 0
    endpoint = _hrana_pipeline_url(url)
    tables = ["jobs", "scores", "artifacts", "runs"]
    requests = [{"type": "execute", "stmt": {"sql": f"SELECT * FROM {tbl}"}} for tbl in tables]
    try:
        body = json.dumps({"requests": requests}).encode()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("Failed to pull data from Turso on startup: %s", exc)
        return 0

    results = data.get("results", [])
    total_restored = 0

    for idx, tbl in enumerate(tables):
        if idx >= len(results):
            break
        res = results[idx].get("response", {}).get("result", {})
        cols = [c["name"] for c in res.get("cols", [])]
        rows = res.get("rows", [])
        if not (cols and rows):
            continue

        placeholders = ",".join(["?"] * len(cols))
        col_names = ",".join(cols)
        sql = f"INSERT OR REPLACE INTO {tbl} ({col_names}) VALUES ({placeholders})"

        values_to_insert = []
        for row in rows:
            extracted = []
            for cell in row:
                t = cell.get("type")
                if t == "null":
                    extracted.append(None)
                elif t == "integer":
                    extracted.append(int(cell.get("value", 0)))
                elif t == "float":
                    extracted.append(float(cell.get("value", 0.0)))
                else:
                    extracted.append(cell.get("value", ""))
            values_to_insert.append(extracted)

        try:
            local_conn.executemany(sql, values_to_insert)
            local_conn.commit()
            total_restored += len(values_to_insert)
            log.info("Restored %d rows for %s from Turso", len(values_to_insert), tbl)
        except Exception as e:
            log.warning("Failed inserting restored rows for %s: %s", tbl, e)

    return total_restored


def push_to_turso(local_conn: sqlite3.Connection) -> int:
    """Push local jobs, scores, artifacts, and runs to Turso cloud database.
    Called automatically after daily runs or custom tailoring to ensure cloud sync.
    """
    if not is_turso_enabled():
        return 0

    tables = ["jobs", "scores", "artifacts", "runs"]
    stmts_to_send: list[tuple[str, list[Any] | None]] = []

    for tbl in tables:
        try:
            rows = local_conn.execute(f"SELECT * FROM {tbl}").fetchall()
            if not rows:
                continue
            cols = rows[0].keys()
            placeholders = ",".join(["?"] * len(cols))
            col_names = ",".join(cols)
            sql = f"INSERT OR REPLACE INTO {tbl} ({col_names}) VALUES ({placeholders})"
            for r in rows:
                stmts_to_send.append((sql, list(r)))
        except Exception as e:
            log.warning("Failed reading %s for Turso push: %s", tbl, e)

    if not stmts_to_send:
        return 0

    chunk_size = 20
    success_count = 0
    for i in range(0, len(stmts_to_send), chunk_size):
        chunk = stmts_to_send[i:i + chunk_size]
        if execute_turso_pipeline(chunk, timeout=30):
            success_count += len(chunk)

    log.info("Synchronized %d records to Turso cloud database", success_count)
    return success_count
