"""Data loading contract for the interactive Nipux terminal frame."""

from __future__ import annotations

from typing import Any

from nipux_cli.config import AppConfig
from nipux_cli.daemon import daemon_lock_status
from nipux_cli.db import AgentDB
from nipux_cli.tui_outcomes import SUMMARY_EVENT_TYPES, SUMMARY_TOOL_EVENT_TYPES, is_summary_event_candidate


def load_frame_snapshot(
    db: AgentDB,
    config: AppConfig,
    job_id: str,
    *,
    default_job_id: str | None = None,
    history_limit: int = 12,
) -> dict[str, Any]:
    """Return the compact state bundle rendered by the chat TUI."""

    resolved_job_id = job_id or default_job_id
    job = db.get_job(resolved_job_id)
    jobs = db.list_jobs()
    token_usage = db.job_token_usage(resolved_job_id)
    token_usage["input_cost_per_million"] = config.model.input_cost_per_million
    token_usage["output_cost_per_million"] = config.model.output_cost_per_million
    summary_events = _summary_events(db, resolved_job_id, history_limit=history_limit)
    return {
        "job_id": resolved_job_id,
        "job": job,
        "jobs": jobs,
        "steps": db.list_steps(job_id=resolved_job_id, limit=80),
        "artifacts": db.list_artifacts(resolved_job_id, limit=8),
        "job_artifacts": {
            str(item["id"]): db.list_artifacts(str(item["id"]), limit=3)
            for item in jobs[:6]
            if item.get("id")
        },
        "job_counts": {
            str(item["id"]): db.job_record_counts(str(item["id"]))
            for item in jobs[:6]
            if item.get("id")
        },
        "memory_entries": db.list_memory(resolved_job_id)[:8],
        "events": db.list_events(job_id=resolved_job_id, limit=max(history_limit * 16, 240)),
        "summary_events": summary_events,
        "daemon": daemon_lock_status(config.runtime.home / "agentd.lock"),
        "model": config.model.model,
        "base_url": config.model.base_url,
        "context_length": config.model.context_length,
        "token_usage": token_usage,
        "counts": db.job_record_counts(resolved_job_id),
    }


def _summary_events(db: AgentDB, job_id: str, *, history_limit: int) -> list[dict[str, Any]]:
    durable_events = db.list_events(
        job_id=job_id,
        limit=max(history_limit * 24, 360),
        event_types=SUMMARY_EVENT_TYPES,
    )
    tool_events = [
        event
        for event in db.list_events(
            job_id=job_id,
            limit=max(history_limit * 10, 160),
            event_types=SUMMARY_TOOL_EVENT_TYPES,
        )
        if is_summary_event_candidate(event)
    ]
    merged: dict[str, dict[str, Any]] = {}
    for event in [*durable_events, *tool_events]:
        event_id = str(event.get("id") or "")
        if event_id:
            merged[event_id] = event
        else:
            merged[f"{event.get('created_at')}-{event.get('event_type')}-{event.get('title')}-{len(merged)}"] = event
    return sorted(merged.values(), key=lambda event: (str(event.get("created_at") or ""), str(event.get("id") or "")))
