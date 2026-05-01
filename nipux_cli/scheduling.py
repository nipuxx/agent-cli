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


def job_provider_blocked(job: dict[str, Any]) -> bool:
    """Return true when provider calls need operator action before retrying."""

    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    blocked_raw = str(metadata.get("provider_blocked_at") or "").strip()
    if not blocked_raw:
        return False
    unblocked_raw = str(metadata.get("provider_unblocked_at") or "").strip()
    if not unblocked_raw:
        return True
    blocked_at = _metadata_time(blocked_raw)
    unblocked_at = _metadata_time(unblocked_raw)
    if blocked_at is None or unblocked_at is None:
        return False
    return blocked_at > unblocked_at


def provider_retry_metadata() -> dict[str, str]:
    """Metadata patch used when the operator explicitly retries provider work."""

    return {
        "provider_blocked_at": "",
        "provider_unblocked_at": datetime.now(timezone.utc).isoformat(),
    }


def _metadata_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
