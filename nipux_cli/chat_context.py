"""Prompt context builder for the Nipux chat-side controller model."""

from __future__ import annotations

from typing import Any

from nipux_cli.db import AgentDB
from nipux_cli.metric_format import format_metric_value
from nipux_cli.operator_context import active_prompt_operator_entries
from nipux_cli.tui_events import clean_step_summary, generic_display_text
from nipux_cli.tui_style import _one_line


def build_chat_messages(db: AgentDB, job: dict[str, Any], message: str) -> list[dict[str, str]]:
    """Build bounded visible-state context for conversational job control."""

    steps = db.list_steps(job_id=job["id"])[-10:]
    artifacts = db.list_artifacts(job["id"], limit=5)
    timeline_events = db.list_timeline_events(job["id"], limit=18)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    operator_messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    agent_updates = metadata.get("agent_updates") if isinstance(metadata.get("agent_updates"), list) else []
    lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
    findings = metadata.get("finding_ledger") if isinstance(metadata.get("finding_ledger"), list) else []
    sources = metadata.get("source_ledger") if isinstance(metadata.get("source_ledger"), list) else []
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    experiments = metadata.get("experiment_ledger") if isinstance(metadata.get("experiment_ledger"), list) else []
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}

    step_lines = "\n".join(
        f"- #{step['step_no']} {step['status']} {step.get('tool_name') or step['kind']}: "
        f"{clean_step_summary(step.get('summary') or step.get('error') or '')}"
        for step in steps
    )
    artifact_lines = "\n".join(
        f"- #{index} {artifact.get('title') or artifact['id']}: {artifact.get('summary') or ''} "
        f"(view with /artifact {index})"
        for index, artifact in enumerate(artifacts, start=1)
    )
    steering_lines = "\n".join(
        f"- {entry.get('source', 'operator')} {entry.get('mode', 'steer')}: {entry.get('message', '')}"
        for entry in active_prompt_operator_entries(operator_messages)[-6:]
        if isinstance(entry, dict)
    )
    update_lines = "\n".join(
        f"- {entry.get('category', 'progress')}: {entry.get('message', '')}"
        for entry in agent_updates[-5:]
        if isinstance(entry, dict)
    )
    lesson_lines = "\n".join(
        f"- {entry.get('category', 'memory')}: {entry.get('lesson', '')}"
        for entry in lessons[-8:]
        if isinstance(entry, dict)
    )
    finding_lines = "\n".join(
        f"- {entry.get('name')}: {entry.get('category') or ''} {entry.get('location') or ''} score={entry.get('score')}"
        for entry in findings[-8:]
        if isinstance(entry, dict)
    )
    task_lines = "\n".join(
        f"- {entry.get('status') or 'open'} p={entry.get('priority') or 0}: {entry.get('title')}"
        for entry in tasks[-10:]
        if isinstance(entry, dict)
    )
    milestone_lines = _roadmap_lines(roadmap)
    experiment_lines = "\n".join(_experiment_line(entry) for entry in experiments[-10:] if isinstance(entry, dict))
    source_lines = "\n".join(
        f"- {entry.get('source')}: score={entry.get('usefulness_score')} "
        f"findings={entry.get('yield_count') or 0} outcome={entry.get('last_outcome') or ''}"
        for entry in sources[-8:]
        if isinstance(entry, dict)
    )
    timeline_lines = "\n".join(_event_line(event, chars=700) for event in timeline_events[-12:])

    sections = {
        "Recent tool calls": _clip_chat_context(step_lines, 1_800),
        "Latest artifacts": _clip_chat_context(artifact_lines, 1_200),
        "Finding ledger": _clip_chat_context(finding_lines, 1_200),
        "Task queue": _clip_chat_context(task_lines, 1_300),
        "Roadmap": _clip_chat_context(milestone_lines, 1_200),
        "Experiment ledger": _clip_chat_context(experiment_lines, 1_300),
        "Source ledger": _clip_chat_context(source_lines, 1_100),
        "Lessons learned": _clip_chat_context(lesson_lines, 1_000),
        "Recent operator steering": _clip_chat_context(steering_lines, 1_200),
        "Recent agent notes": _clip_chat_context(update_lines, 1_200),
        "Recent visible timeline": _clip_chat_context(timeline_lines, 1_800),
    }
    section_text = "\n\n".join(f"{title}:\n{body or _empty_section_text(title)}" for title, body in sections.items())
    return [
        {
            "role": "system",
            "content": (
                "You are Nipux, the chat model that controls a generic long-running agent workspace. "
                "You know the visible CLI state, focused job, job list, task queue, artifacts, memory, metrics, and recent activity. "
                "Answer directly from the visible job state. Do not claim hidden chain-of-thought. "
                "If the operator asks for work to be done, explain the concrete job/control action Nipux will take or how to run it from the Jobs/Status panel. "
                "If the operator asks where saved work is, explain that artifacts and history are visible from the Jobs/Status panel or direct CLI commands. "
                "Do not start replies with an introduction. Keep replies concise and useful."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Job title: {job['title']}\n"
                f"Job status: {job['status']}\n"
                f"Kind: {job['kind']}\n"
                f"Objective: {job['objective']}\n\n"
                f"{section_text}\n\n"
                f"Operator message:\n{message}"
            ),
        },
    ]


def _roadmap_lines(roadmap: dict[str, Any]) -> str:
    if not roadmap:
        return ""
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    body = "\n".join(
        (
            f"- {entry.get('status') or 'planned'} validation={entry.get('validation_status') or 'not_started'} "
            f"p={entry.get('priority') or 0}: {entry.get('title')}"
        )
        for entry in milestones[-8:]
        if isinstance(entry, dict)
    )
    header = (
        f"{roadmap.get('status') or 'planned'}: {roadmap.get('title') or 'Roadmap'}"
        + (f" current={roadmap.get('current_milestone')}" if roadmap.get("current_milestone") else "")
    )
    return f"{header}\n{body}".strip()


def _experiment_line(entry: dict[str, Any]) -> str:
    if entry.get("metric_value") is None:
        return f"- {entry.get('status') or 'planned'}: {entry.get('title')}"
    metric = format_metric_value(
        entry.get("metric_name") or "metric",
        entry.get("metric_value"),
        entry.get("metric_unit") or "",
    )
    return (
        f"- {entry.get('status') or 'planned'}: {entry.get('title')}"
        f" {metric}"
        f"{' best' if entry.get('best_observed') else ''}"
    )


def _event_line(event: dict[str, Any], *, chars: int) -> str:
    when = _compact_time(str(event.get("created_at") or "?"))
    label = _event_label(str(event.get("event_type") or "event"), event.get("metadata"))
    title = str(event.get("title") or "").strip()
    body = generic_display_text(event.get("body") or "")
    detail = title if title else str(event.get("event_type") or "event")
    if body:
        detail = f"{detail} - {clean_step_summary(body)}"
    return f"{when:<16} {label:<8} {_one_line(detail, chars)}"


def _event_label(kind: str, metadata: Any) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    if kind == "operator_message":
        return "FOLLOW" if str(metadata.get("mode") or "") == "follow_up" else "USER"
    if kind == "agent_message":
        return "AGENT"
    if kind == "tool_call":
        return "TOOL"
    if kind == "tool_result":
        status = str(metadata.get("status") or "")
        if status == "blocked":
            return "BLOCK"
        if status == "failed":
            return "ERROR"
        return "DONE"
    labels = {
        "artifact": "OUTPUT",
        "compaction": "MEMORY",
        "digest": "DIGEST",
        "error": "ERROR",
        "experiment": "TEST",
        "finding": "FIND",
        "lesson": "LEARN",
        "milestone_validation": "VALID",
        "operator_context": "ACK",
        "reflection": "PLAN",
        "roadmap": "ROAD",
        "source": "SOURCE",
        "task": "TASK",
    }
    return labels.get(kind, kind.upper()[:8])


def _compact_time(value: str) -> str:
    text = value.replace("T", " ")
    if len(text) >= 16 and text[4:5] == "-" and text[13:14] == ":":
        return text[:16]
    return text[:16]


def _empty_section_text(title: str) -> str:
    return "None." if title.startswith("Recent operator") else "None yet."


def _clip_chat_context(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    marker = f"\n... clipped {len(text) - limit} chars from this visible state section ..."
    return text[: max(0, limit - len(marker))].rstrip() + marker
