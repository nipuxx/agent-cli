"""Readable durable progress reports for jobs."""

from __future__ import annotations

import shlex
from typing import Any

from nipux_cli.config import AppConfig
from nipux_cli.daemon import daemon_lock_status
from nipux_cli.db import AgentDB
from nipux_cli.tui_outcomes import hourly_update_lines
from nipux_cli.tui_status import job_display_state
from nipux_cli.tui_style import _one_line


def render_updates_report(
    db: AgentDB,
    config: AppConfig,
    job_id: str,
    *,
    limit: int = 5,
    chars: int = 180,
    paths: bool = False,
) -> list[str]:
    job = db.get_job(job_id)
    artifacts = db.list_artifacts(job_id, limit=limit)
    events = db.list_timeline_events(job_id, limit=max(250, limit * 80))
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    operator_messages = _metadata_list(metadata, "operator_messages")
    agent_updates = _metadata_list(metadata, "agent_updates")
    lessons = _metadata_list(metadata, "lessons")
    daemon = daemon_lock_status(config.runtime.home / "agentd.lock")
    lines = [
        f"updates {job['title']} | state {job_display_state(job, bool(daemon['running']))}",
        "=" * 80,
    ]
    if operator_messages:
        latest = operator_messages[-1]
        lines.append(f"last steering: {_one_line(latest.get('message') or '', chars)}")
    lines.append("outcomes by hour:")
    outcome_lines = hourly_update_lines(events, width=max(72, chars), limit=max(8, limit * 4))
    if outcome_lines:
        lines.extend(f"  {line}" for line in outcome_lines)
    else:
        lines.append("  none yet")
    if agent_updates:
        lines.extend(["", "latest agent notes:"])
        for update in agent_updates[-min(limit, 5) :]:
            category = update.get("category") or "progress"
            lines.append(f"  {category}: {_one_line(update.get('message') or '', chars)}")
    if lessons:
        lines.extend(["", "latest lessons:"])
        for lesson in lessons[-min(limit, 5) :]:
            category = lesson.get("category") or "memory"
            lines.append(f"  {category}: {_one_line(lesson.get('lesson') or '', chars)}")
    lines.extend(["", "latest saved outputs:"])
    if not artifacts:
        lines.append("  none yet")
    for artifact in artifacts:
        title = artifact.get("title") or artifact["id"]
        summary = f" - {_one_line(artifact['summary'], chars)}" if artifact.get("summary") else ""
        lines.append(f"  {artifact['created_at']} {title}{summary}")
        lines.append(f"    view: artifact {shlex.quote(title)}")
        if paths:
            lines.append(f"    {artifact['path']}")
    lines.extend(["", "raw tool stream: activity"])
    return lines


def _metadata_list(metadata: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = metadata.get(key)
    return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []
