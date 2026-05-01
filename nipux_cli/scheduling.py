"""Shared scheduling helpers for deferred long-running work."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def job_deferred_until(job: dict[str, Any], *, now: datetime | None = None) -> datetime | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    raw_until = str(metadata.get("defer_until") or "").strip()
    if not raw_until:
        return None
    try:
        until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    until = until.astimezone(timezone.utc)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return until if until > now else None


def job_is_deferred(job: dict[str, Any], *, now: datetime | None = None) -> bool:
    return job_deferred_until(job, now=now) is not None
