"""Cloud storage helper: Supabase Storage for generated resume documents."""
from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path
from typing import Any
import urllib.request
import certifi

log = logging.getLogger("jobhunter.storage")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "resumes")


def is_cloud_storage_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def upload_artifact(file_path: str | Path, subpath: str) -> str | None:
    """Upload a file to Supabase Storage and return its public URL.
    Returns None if cloud storage is disabled or if upload fails.
    """
    if not is_cloud_storage_enabled():
        return None

    path = Path(file_path)
    if not path.is_file():
        return None

    rel_name = subpath.lstrip("/")
    endpoint = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{rel_name}"

    content_types = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".html": "text/html; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".json": "application/json",
    }
    content_type = content_types.get(path.suffix.lower(), "application/octet-stream")

    try:
        data = path.read_bytes()
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": content_type,
            "x-upsert": "true",
        }
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            if resp.status in (200, 201):
                public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{rel_name}"
                log.info("Uploaded %s -> %s", path.name, public_url)
                return public_url
    except Exception as exc:
        log.warning("Supabase storage upload failed for %s: %s", path.name, exc)
    return None
