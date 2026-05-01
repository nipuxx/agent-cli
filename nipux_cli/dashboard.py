"""Operator-facing dashboard state and rendering."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from textwrap import shorten
from typing import Any

from nipux_cli.config import AppConfig
from nipux_cli.daemon import daemon_lock_status
from nipux_cli.db import AgentDB
from nipux_cli.operator_context import active_prompt_operator_entries
from nipux_cli.scheduling import job_deferred_until
from nipux_cli.tools import DEFAULT_REGISTRY


def collect_dashboard_state(
    db: AgentDB,
    config: AppConfig,
    *,
    job_id: str | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """Build a serializable snapshot for status and dashboard commands."""

    jobs = db.list_jobs()
    selected = _select_focus_job(db, jobs, job_id)
    job_cards = [_job_card(db, job) for job in jobs]
    focus = _focus_state(db, selected, limit=limit) if selected else None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "daemon": daemon_lock_status(config.runtime.home / "agentd.lock"),
        "runtime": {
            "home": str(config.runtime.home),
            "state_db": str(config.runtime.state_db_path),
            "logs_dir": str(config.runtime.logs_dir),
            "model": config.model.model,
            "base_url": config.model.base_url,
            "tool_count": len(DEFAULT_REGISTRY.names()),
        },
        "jobs": job_cards,
        "focus": focus,
    }


def render_dashboard(state: dict[str, Any], *, width: int = 120, chars: int = 260) -> str:
    """Render a compact terminal dashboard."""

    width = max(72, min(width, 160))
    line = "-" * width
    runtime = state["runtime"]
    daemon = state["daemon"]
    focus = state.get("focus")
    generated_at = _compact_time(state["generated_at"])
    daemon_text = _daemon_text(daemon)
    lines = [
        "Nipux CLI Dashboard".ljust(width - len(generated_at)) + generated_at,
        line,
        f"daemon: {daemon_text}",
        f"model: {runtime['model']} | endpoint: {runtime['base_url']} | tools: {runtime['tool_count']}",
        f"home: {runtime['home']}",
        "trace: model-visible state, tool calls, outputs, artifacts, and errors. Hidden chain-of-thought is not exposed.",
        line,
        "Jobs",
    ]
    jobs = state.get("jobs") or []
    if not jobs:
        lines.append("  no jobs yet")
    else:
        lines.append("  title                         state      kind            steps  artifacts  last action")
        for job in jobs[:12]:
            latest = job.get("latest_step") or {}
            last_action = _one_line(latest.get("summary") or latest.get("error") or "-", 42)
            display_state = _job_state_text(job, bool(daemon.get("running")))
            lines.append(
                f"  {_one_line(job['title'], 29):<29} {display_state:<10} {job['kind']:<15} "
                f"{job['step_count']:>5} {job['artifact_count']:>10}  {last_action}"
            )
    if focus:
        lines.extend(_render_focus(focus, width=width, chars=chars, daemon_running=bool(daemon.get("running"))))
    return "\n".join(lines).rstrip() + "\n"


def render_overview(state: dict[str, Any], *, width: int = 100) -> str:
    """Render a human-sized status view for the interactive shell."""

    width = max(72, min(width, 120))
    runtime = state["runtime"]
    daemon = state["daemon"]
    focus = state.get("focus")
    jobs = state.get("jobs") or []
    latest_step = ((focus or {}).get("recent_steps") or [{}])[-1] if focus else {}
    lines = [
        "Nipux Status",
        "=" * min(width, 96),
        f"daemon: {_daemon_health_text(daemon, latest_step=latest_step)}",
        f"model: {runtime['model']}",
        f"jobs: {len(jobs)} total | tools: {runtime['tool_count']} | home: {runtime['home']}",
    ]
    if not focus:
        lines.append("focus: no job yet")
        lines.append("")
        lines.append("next: create \"your objective\" --title \"name\"")
        return "\n".join(lines).rstrip() + "\n"

    job = focus["job"]
    counts = focus["counts"]
    artifacts = focus.get("artifacts") or []
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    operator = (metadata.get("last_operator_message") if isinstance(metadata, dict) else None) or {}
    agent_update = (metadata.get("last_agent_update") if isinstance(metadata, dict) else None) or {}
    lesson = (metadata.get("last_lesson") if isinstance(metadata, dict) else None) or {}
    findings = metadata.get("finding_ledger") if isinstance(metadata.get("finding_ledger"), list) else []
    sources = metadata.get("source_ledger") if isinstance(metadata.get("source_ledger"), list) else []
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    experiments = metadata.get("experiment_ledger") if isinstance(metadata.get("experiment_ledger"), list) else []
    active_operator = _active_operator_messages(metadata)
    pending_measurement = metadata.get("pending_measurement_obligation") if isinstance(metadata.get("pending_measurement_obligation"), dict) else {}
    lines.extend([
        "",
        f"focus: {job['title']}",
        (
            f"state: {_job_state_text(job, bool(daemon.get('running')))} | "
            f"worker: {_worker_text(job, bool(daemon.get('running')))} | kind: {job['kind']} | "
            f"steps: {counts['steps']} | artifacts: {counts['artifacts']} | failures: {counts['failed_steps']}"
        ),
        f"learning: findings={len(findings)} | sources={len(sources)} | tasks={len(tasks)} | experiments={len(experiments)} | lessons={counts.get('lessons', 0)} | reflections={counts.get('reflections', 0)}",
        f"objective: {_one_line(job['objective'], width - 11)}",
    ])
    if active_operator:
        lines.append(f"operator context: {len(active_operator)} active | {_one_line(active_operator[-1].get('message') or '', width - 28)}")
    if pending_measurement:
        lines.append(f"measurement: pending from step #{pending_measurement.get('source_step_no') or '?'}")
    if latest_step:
        tool = latest_step.get("tool_name") or latest_step.get("kind") or "-"
        status = latest_step.get("status") or "-"
        summary = latest_step.get("summary") or latest_step.get("error") or "-"
        lines.append(f"latest: #{latest_step.get('step_no')} {status} {tool}: {_one_line(summary, width - 22)}")
    if artifacts:
        artifact = artifacts[0]
        lines.append(f"latest artifact: {artifact.get('title') or artifact['id']}")
    if operator:
        lines.append(f"last steering: {_one_line(operator.get('message') or '', width - 15)}")
    if agent_update:
        lines.append(f"agent note: {_one_line(agent_update.get('message') or '', width - 12)}")
    if lesson:
        lines.append(f"latest lesson: {_one_line(lesson.get('lesson') or '', width - 16)}")
    lines.extend([
        "",
        "commands: activity | updates | findings | tasks | sources | memory | metrics | work --steps 3 | start | stop",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _select_focus_job(db: AgentDB, jobs: list[dict[str, Any]], job_id: str | None) -> dict[str, Any] | None:
    if job_id:
        return db.get_job(job_id)
    for status in ("running", "queued", "paused", "failed", "completed"):
        for job in jobs:
            if job.get("status") == status:
                return job
    return jobs[0] if jobs else None


def _job_card(db: AgentDB, job: dict[str, Any]) -> dict[str, Any]:
    steps = db.list_steps(job_id=job["id"])
    artifacts = db.list_artifacts(job["id"], limit=500)
    runs = db.list_runs(job["id"], limit=500)
    return {
        "id": job["id"],
        "status": job["status"],
        "kind": job["kind"],
        "title": job["title"],
        "updated_at": job["updated_at"],
        "step_count": _step_count(steps),
        "run_count": len(runs),
        "failed_steps": sum(1 for step in steps if step.get("status") == "failed"),
        "artifact_count": len(artifacts),
        "latest_step": _public_step(steps[-1]) if steps else None,
    }


def _focus_state(db: AgentDB, job: dict[str, Any], *, limit: int) -> dict[str, Any]:
    steps = db.list_steps(job_id=job["id"])
    runs = db.list_runs(job["id"], limit=limit)
    artifacts = db.list_artifacts(job["id"], limit=limit)
    memory = db.list_memory(job["id"])
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
    findings = metadata.get("finding_ledger") if isinstance(metadata.get("finding_ledger"), list) else []
    sources = metadata.get("source_ledger") if isinstance(metadata.get("source_ledger"), list) else []
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    experiments = metadata.get("experiment_ledger") if isinstance(metadata.get("experiment_ledger"), list) else []
    reflections = metadata.get("reflections") if isinstance(metadata.get("reflections"), list) else []
    active_operator = _active_operator_messages(metadata)
    tool_counts = Counter(step.get("tool_name") or step.get("kind") or "unknown" for step in steps)
    blocked = [step for step in steps if str(step.get("error") or "").endswith("blocked") or "blocked" in str(step.get("summary") or "")]
    return {
        "job": {
            "id": job["id"],
            "title": job["title"],
            "kind": job["kind"],
            "status": job["status"],
            "objective": job["objective"],
            "updated_at": job["updated_at"],
            "metadata": job.get("metadata") or {},
        },
        "counts": {
            "steps": _step_count(steps),
            "runs": len(db.list_runs(job["id"], limit=1000)),
            "artifacts": len(db.list_artifacts(job["id"], limit=1000)),
            "failed_steps": sum(1 for step in steps if step.get("status") == "failed"),
            "blocked_steps": len(blocked),
            "findings": len(findings),
            "sources": len(sources),
            "tasks": len(tasks),
            "experiments": len(experiments),
            "active_operator_messages": len(active_operator),
            "lessons": len(lessons),
            "reflections": len(reflections),
        },
        "tool_counts": dict(tool_counts.most_common(8)),
        "recent_runs": [_public_run(run) for run in runs],
        "recent_steps": [_public_step(step) for step in steps[-limit:]],
        "artifacts": [_public_artifact(artifact) for artifact in artifacts],
        "memory": [
            {
                "key": entry.get("key"),
                "summary": entry.get("summary"),
                "artifact_refs": entry.get("artifact_refs") or [],
                "updated_at": entry.get("updated_at"),
            }
            for entry in memory[:4]
        ],
        "lessons": [
            {
                "at": entry.get("at"),
                "category": entry.get("category") or "memory",
                "lesson": entry.get("lesson") or "",
                "confidence": entry.get("confidence"),
            }
            for entry in lessons[-5:]
            if isinstance(entry, dict)
        ],
        "findings": findings[-8:],
        "tasks": tasks[-12:],
        "experiments": experiments[-12:],
        "active_operator_messages": active_operator[-12:],
        "sources": sources[-8:],
        "reflections": reflections[-4:],
    }


def _render_focus(focus: dict[str, Any], *, width: int, chars: int, daemon_running: bool) -> list[str]:
    job = focus["job"]
    counts = focus["counts"]
    lines = [
        "-" * width,
        f"Focus Job: {job['title']} | state {_job_state_text(job, daemon_running)} | {job['kind']}",
        f"objective: {_one_line(job['objective'], width - 11)}",
        (
            f"counts: steps={counts['steps']} runs={counts['runs']} artifacts={counts['artifacts']} "
            f"failed_steps={counts['failed_steps']} blocked_steps={counts['blocked_steps']}"
        ),
        f"learning: findings={counts.get('findings', 0)} sources={counts.get('sources', 0)} tasks={counts.get('tasks', 0)} experiments={counts.get('experiments', 0)} lessons={counts.get('lessons', 0)} reflections={counts.get('reflections', 0)}",
        f"tool mix: {_tool_mix(focus.get('tool_counts') or {})}",
    ]
    active_operator = focus.get("active_operator_messages") or []
    if active_operator:
        lines.append(f"operator context: {len(active_operator)} active | {_one_line(active_operator[-1].get('message') or '', chars)}")
    pending_measurement = (job.get("metadata") or {}).get("pending_measurement_obligation") if isinstance(job.get("metadata"), dict) else {}
    if isinstance(pending_measurement, dict) and pending_measurement:
        lines.append(f"measurement obligation: pending from step #{pending_measurement.get('source_step_no') or '?'}")
    lines.extend(["", "Recent Steps"])
    recent_steps = focus.get("recent_steps") or []
    if not recent_steps:
        lines.append("  no steps recorded")
    for step in recent_steps:
        error = f" | error={_one_line(step['error'], 70)}" if step.get("error") else ""
        lines.append(
            f"  #{step['step_no']:<4} {step['status']:<9} {step.get('tool_name') or step['kind']:<18} "
            f"{_one_line(step.get('summary') or '-', chars)}{error}"
        )
        args = step.get("arguments") or {}
        if args:
            lines.append(f"       args: {_one_line(_compact_value(args), chars)}")
    lines.append("")
    lines.append("Artifacts")
    artifacts = focus.get("artifacts") or []
    if not artifacts:
        lines.append("  no artifacts yet")
    for artifact in artifacts[:8]:
        title = artifact.get("title") or artifact["id"]
        lines.append(f"  {artifact['created_at']} {artifact['type']} {title}")
        if artifact.get("summary"):
            lines.append(f"       {_one_line(artifact['summary'], chars)}")
    lessons = focus.get("lessons") or []
    if lessons:
        lines.append("")
        lines.append("Lessons")
        for lesson in lessons:
            lines.append(f"  {lesson.get('category') or 'memory'}: {_one_line(lesson.get('lesson') or '', chars)}")
    findings = focus.get("findings") or []
    if findings:
        lines.append("")
        lines.append("Recent Findings")
        for finding in findings[-5:]:
            lines.append(f"  {_one_line(finding.get('name') or 'unknown', 48)} score={finding.get('score')} {finding.get('category') or ''}")
    tasks = focus.get("tasks") or []
    if tasks:
        lines.append("")
        lines.append("Task Queue")
        for task in tasks[-6:]:
            lines.append(f"  {task.get('status') or 'open':<7} p={task.get('priority') or 0:<3} {_one_line(task.get('title') or 'untitled', 56)}")
    sources = focus.get("sources") or []
    if sources:
        lines.append("")
        lines.append("Recent Sources")
        for source in sources[-5:]:
            lines.append(f"  {_one_line(source.get('source') or 'unknown', 48)} score={source.get('usefulness_score')} findings={source.get('yield_count') or 0}")
    memory = focus.get("memory") or []
    if memory:
        lines.append("")
        lines.append("Compact Memory")
        for entry in memory:
            refs = ", ".join(entry.get("artifact_refs") or [])
            suffix = f" refs={refs}" if refs else ""
            lines.append(f"  {entry['key']}: {_one_line(entry.get('summary') or '', chars)}{suffix}")
    return lines


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run["id"],
        "status": run["status"],
        "started_at": run["started_at"],
        "ended_at": run.get("ended_at"),
        "model": run.get("model"),
        "error": run.get("error"),
    }


def _public_step(step: dict[str, Any]) -> dict[str, Any]:
    input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
    args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
    return {
        "id": step["id"],
        "step_no": step["step_no"],
        "kind": step["kind"],
        "status": step["status"],
        "tool_name": step.get("tool_name"),
        "started_at": step["started_at"],
        "ended_at": step.get("ended_at"),
        "summary": _clean_step_summary(step.get("summary")),
        "error": step.get("error"),
        "arguments": args,
    }


def _public_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": artifact["id"],
        "created_at": artifact["created_at"],
        "type": artifact["type"],
        "title": artifact.get("title"),
        "summary": artifact.get("summary"),
        "path": artifact["path"],
    }


def _step_count(steps: list[dict[str, Any]]) -> int:
    numbers = [int(step.get("step_no") or 0) for step in steps]
    return max(numbers, default=0)


def _active_operator_messages(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    prompt_entries = active_prompt_operator_entries(messages)
    return [
        entry for entry in messages
        if isinstance(entry, dict)
        and entry in prompt_entries
        and str(entry.get("mode") or "steer") in {"steer", "follow_up"}
    ]


def _daemon_text(daemon: dict[str, Any]) -> str:
    metadata = daemon.get("metadata") or {}
    if daemon.get("running"):
        pid = metadata.get("pid") or "unknown"
        started = metadata.get("started_at") or "unknown start"
        stale = " stale-runtime" if daemon.get("stale") else ""
        return f"running pid={pid}{stale} started={started}"
    return "stopped"


def _daemon_health_text(daemon: dict[str, Any], *, latest_step: dict[str, Any] | None = None) -> str:
    if not daemon.get("running"):
        return "stopped (job will not advance until you run: start)"
    metadata = daemon.get("metadata") or {}
    heartbeat = metadata.get("last_heartbeat")
    status = "running"
    if daemon.get("stale"):
        status = "running stale-runtime"
    if heartbeat:
        age = _age_seconds(heartbeat)
        if age is not None:
            status += f" | heartbeat {int(age)}s ago"
            running_step = latest_step or {}
            if age > 120 and running_step.get("status") == "running":
                tool = running_step.get("tool_name") or running_step.get("kind") or "step"
                step_age = _age_seconds(running_step.get("started_at") or "")
                if step_age is not None:
                    status += f" | busy #{running_step.get('step_no')} {tool} for {int(step_age)}s"
                else:
                    status += f" | busy #{running_step.get('step_no')} {tool}"
            elif age > 120:
                status += " (stale)"
    failures = metadata.get("consecutive_failures")
    if failures:
        status += f" | consecutive failures: {failures}"
    tool = metadata.get("last_tool")
    step_status = metadata.get("last_status")
    if tool or step_status:
        status += f" | last: {step_status or '?'} {tool or '-'}"
    if metadata.get("last_error"):
        status += f" | error: {_one_line(metadata.get('last_error'), 48)}"
    return status


def _worker_text(job: dict[str, Any], daemon_running: bool) -> str:
    status = str(job.get("status") or "")
    if status in {"paused", "completed", "cancelled", "failed"}:
        return status
    if job_deferred_until(job):
        return "waiting"
    return "active" if daemon_running and status in {"running", "queued"} else "idle"


def _job_state_text(job: dict[str, Any], daemon_running: bool) -> str:
    status = str(job.get("status") or "")
    if status in {"running", "queued"}:
        if job_deferred_until(job):
            return "waiting"
        return "advancing" if daemon_running else "open"
    return status or "unknown"


def _age_seconds(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def _compact_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _one_line(value: Any, width: int) -> str:
    text = " ".join(str(value).split())
    return shorten(text, width=max(8, width), placeholder="...")


def _clean_step_summary(summary: Any) -> str:
    text = " ".join(str(summary or "").split())
    if text.startswith("write_artifact saved ") and " at /" in text:
        return text.split(" at /", 1)[0]
    return text


def _compact_value(value: Any) -> str:
    if isinstance(value, dict):
        parts = [f"{key}={value[key]!r}" for key in sorted(value)]
        return ", ".join(parts)
    return str(value)


def _tool_mix(tool_counts: dict[str, int]) -> str:
    if not tool_counts:
        return "none"
    return ", ".join(f"{name}:{count}" for name, count in tool_counts.items())


def resolve_artifact_path(path: str | Path) -> str:
    return str(Path(path).expanduser())
