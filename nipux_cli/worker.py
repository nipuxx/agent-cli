"""Bounded worker loop for one restartable agent step."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, load_config
from nipux_cli.compression import refresh_memory_index
from nipux_cli.db import AgentDB
from nipux_cli.llm import LLMResponse, LLMResponseError, OpenAIChatLLM, StepLLM
from nipux_cli.measurement import measurement_candidates, measurement_candidates_are_diagnostic_only
from nipux_cli.metric_format import format_metric_value
from nipux_cli.operator_context import (
    active_prompt_operator_entries,
    inactive_prompt_operator_ids,
    operator_entry_is_prompt_relevant,
)
from nipux_cli.progress import build_progress_checkpoint
from nipux_cli.provider_errors import provider_action_required_note
from nipux_cli.source_quality import anti_bot_reason
from nipux_cli.tools import DEFAULT_REGISTRY, ToolContext, ToolRegistry
from nipux_cli.worker_policy import (
    ACTIVITY_STAGNATION_BLOCKED_TOOLS,
    ACTIVITY_STAGNATION_CHECKPOINTS,
    ANTI_BOT_ACK_TERMS,
    ARTIFACT_ACCOUNTING_BLOCKED_TOOLS,
    ARTIFACT_ACCOUNTING_RESOLUTION_TOOLS,
    BRANCH_WORK_TOOLS,
    CHURN_TOOLS,
    DELIVERABLE_ARTIFACT_TERMS,
    EVIDENCE_ARTIFACT_TERMS,
    EXPERIMENT_DELIVERY_ACTION_TERMS,
    EXPERIMENT_INFORMATION_ACTION_TERMS,
    EXPERIMENT_NEXT_ACTION_BLOCKED_TOOLS,
    LEDGER_PROGRESS_TOOLS,
    MAX_WORKER_PROMPT_CHARS,
    MEASURABLE_ACTION_BUDGET_STEPS,
    MEASURABLE_PROGRESS_PATTERN,
    MEASURABLE_RESEARCH_BLOCKED_TOOLS,
    MEASURABLE_RESEARCH_BUDGET_STEPS,
    MEASUREMENT_BLOCKED_TOOLS,
    MEASUREMENT_RESOLUTION_TOOLS,
    MEMORY_ENTRY_PROMPT_CHARS,
    MEMORY_PROMPT_CHARS,
    MILESTONE_VALIDATION_BLOCKED_TOOLS,
    PROGRAM_PROMPT_CHARS,
    PROMPT_SECTION_BUDGETS,
    QUERY_STOPWORDS,
    READ_ONLY_SHELL_COMMAND_PATTERN,
    RECENT_STATE_PROMPT_CHARS,
    RECENT_STATE_STEPS,
    RECOVERABLE_GUARD_ERRORS,
    REFLECTION_INTERVAL_STEPS,
    ROADMAP_STALENESS_BLOCKED_TOOLS,
    SECTION_ITEM_CHARS,
    SYSTEM_PROMPT,
    TASK_DELIVERABLE_ACTION_TERMS,
    TASK_QUEUE_SATURATION_OPEN_TASKS,
    TEXT_TOKEN_STOPWORDS,
    TIMELINE_PROMPT_AGENT_TITLES,
    TIMELINE_PROMPT_EVENT_TYPES,
    TIMELINE_PROMPT_EVENTS,
    TIMELINE_PROMPT_TOOL_STATUSES,
)
from nipux_cli.worker_prompt_format import (
    clip_text as _clip_text,
    format_step_for_prompt as _format_step_for_prompt,
    observation_for_prompt as _observation_for_prompt,
)
from nipux_cli.worker_usage import turn_usage_metadata

@dataclass(frozen=True)
class StepExecution:
    job_id: str
    run_id: str
    step_id: str
    tool_name: str | None
    status: str
    result: dict[str, Any]


def build_messages(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    memory_entries: list[dict[str, Any]] | None = None,
    program_text: str = "",
    timeline_events: list[dict[str, Any]] | None = None,
    active_operator_messages: list[dict[str, Any]] | None = None,
    include_unclaimed_operator_messages: bool = True,
) -> list[dict[str, Any]]:
    step_lines = []
    for step in recent_steps[-RECENT_STATE_STEPS:]:
        step_lines.append(_clip_text(_format_step_for_prompt(step), 720))
    state = _clip_text("\n".join(step_lines), RECENT_STATE_PROMPT_CHARS) if step_lines else "No prior steps."
    memory_lines = []
    for entry in _memory_entries_for_prompt(memory_entries or []):
        refs = ", ".join((entry.get("artifact_refs") or [])[:8])
        suffix = f"\nArtifact refs: {refs}" if refs else ""
        memory_lines.append(
            _clip_text(f"### {entry.get('key') or 'memory'}\n{entry.get('summary') or ''}{suffix}", MEMORY_ENTRY_PROMPT_CHARS)
        )
    memory_text = _clip_text("\n\n".join(memory_lines), MEMORY_PROMPT_CHARS) if memory_lines else "No compact memory yet."
    program = _clip_text(program_text.strip(), PROGRAM_PROMPT_CHARS) if program_text else "No program.md saved yet."
    operator_messages = _operator_messages_for_prompt(
        job,
        active_messages=active_operator_messages or [],
        include_unclaimed=include_unclaimed_operator_messages,
    )
    measurement_obligation = _measurement_obligation_for_prompt(job)
    measured_progress_guard = _measured_progress_guard_for_prompt(job, recent_steps)
    progress_accounting_guard = _progress_accounting_for_prompt(recent_steps)
    activity_stagnation = _activity_stagnation_for_prompt(job)
    lessons = _lessons_for_prompt(job)
    roadmap = _roadmap_for_prompt(job)
    tasks = _tasks_for_prompt(job)
    ledgers = _ledgers_for_prompt(job)
    experiments = _experiments_for_prompt(job)
    reflections = _reflections_for_prompt(job)
    timeline = _timeline_for_prompt(timeline_events or [])
    next_constraint = _next_action_constraint(job, recent_steps)
    content = _render_worker_prompt(
        job,
        sections=[
            (
                "Workspace",
                "\n".join([
                    "- shell_exec runs on the machine hosting this Nipux worker, in the current worker directory unless the command changes it",
                    "- saved artifacts are separate Nipux outputs; read_artifact is only for those saved outputs",
                    "- use shell_exec for workspace/project files unless the file is a saved artifact",
                ]),
            ),
            ("Operator context", operator_messages),
            ("Pending measurement obligation", measurement_obligation),
            ("Measured progress guard", measured_progress_guard),
            ("Progress accounting guard", progress_accounting_guard),
            ("Activity stagnation", activity_stagnation),
            ("Program", program),
            ("Lessons learned", lessons),
            ("Roadmap", roadmap),
            ("Task queue", tasks),
            ("Ledgers", ledgers),
            ("Experiment ledger", experiments),
            ("Reflections", reflections),
            ("Compact memory", memory_text),
            ("Recent visible timeline", timeline),
            ("Recent state", state),
            ("Next-action constraint", next_constraint),
        ],
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _memory_entries_for_prompt(memory_entries: list[dict[str, Any]], *, limit: int = 2) -> list[dict[str, Any]]:
    entries = [entry for entry in memory_entries if isinstance(entry, dict)]
    rolling = next((entry for entry in entries if entry.get("key") == "rolling_state"), None)
    selected: list[dict[str, Any]] = []
    if rolling:
        selected.append(rolling)
    for entry in entries:
        if len(selected) >= limit:
            break
        if rolling is not None and entry is rolling:
            continue
        selected.append(entry)
    return selected[:limit]


def _render_worker_prompt(job: dict[str, Any], *, sections: list[tuple[str, str]]) -> str:
    objective = _clip_text(job.get("objective") or "", 2_000)
    header = f"Job: {job['title']}\nKind: {job['kind']}\nObjective:\n{objective}"
    instruction = (
        "Take exactly one bounded next action. If recent state contains search results, do not search the same query again. "
        "If recent state contains extracted page evidence, write an artifact before doing more search or browsing."
    )
    scale = 1.0
    while True:
        parts = [header]
        for title, body in sections:
            base_budget = PROMPT_SECTION_BUDGETS.get(title, SECTION_ITEM_CHARS)
            budget = max(260, int(base_budget * scale))
            parts.append(f"{title}:\n{_clip_text(body, budget)}")
        parts.append(instruction)
        content = "\n\n".join(parts)
        if len(content) <= MAX_WORKER_PROMPT_CHARS or scale <= 0.45:
            break
        scale -= 0.12
    if len(content) <= MAX_WORKER_PROMPT_CHARS:
        return content
    suffix_sections: list[str] = []
    for title, body in sections:
        if title == "Operator context":
            suffix_sections.append(f"Operator context:\n{_clip_text(body, 900)}")
        elif title == "Next-action constraint":
            suffix_sections.append(f"Next-action constraint:\n{_clip_text(body, 900)}")
    suffix = "\n\n".join(suffix_sections + [instruction])
    marker = "\n\n...[middle context clipped; operator context and next action repeated below]...\n"
    head_budget = max(0, MAX_WORKER_PROMPT_CHARS - len(suffix) - len(marker))
    return _clip_text(content, head_budget) + marker + suffix


def _operator_messages_for_prompt(
    job: dict[str, Any],
    *,
    active_messages: list[dict[str, Any]] | None = None,
    include_unclaimed: bool = True,
) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    lines = []
    active_messages = active_prompt_operator_entries(active_messages or [])
    active_ids = {active.get("event_id") for active in active_messages if isinstance(active, dict)}
    if active_messages:
        lines.append("Newly delivered operator messages for this turn:")
    for entry in active_messages:
        line = _operator_message_line(entry)
        if line:
            lines.append(line)
    active_context = [
        entry
        for entry in messages
        if isinstance(entry, dict)
        and operator_entry_is_prompt_relevant(entry)
        and (include_unclaimed or entry.get("claimed_at") or str(entry.get("mode") or "steer") == "note")
        and entry.get("event_id") not in active_ids
    ]
    if active_context:
        if lines:
            lines.append("Still-active durable operator context:")
        for entry in active_context[-6:]:
            line = _operator_message_line(entry)
            if line:
                lines.append(line)
    return "\n".join(lines) if lines else "No active operator context."


def _acknowledge_non_prompt_operator_context(db: AgentDB, job_id: str) -> int:
    job = db.get_job(job_id)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    message_ids = inactive_prompt_operator_ids(messages)
    if not message_ids:
        return 0
    result = db.acknowledge_operator_messages(
        job_id,
        message_ids=message_ids,
        summary="conversation-only message retained in history, not used as worker constraint",
    )
    return int(result.get("count") or 0)


def _operator_message_line(entry: dict[str, Any]) -> str:
    if not isinstance(entry, dict):
        return ""
    at = str(entry.get("at") or "")
    source = str(entry.get("source") or "operator")
    mode = str(entry.get("mode") or "steer")
    event_id = str(entry.get("event_id") or "")
    message = " ".join(str(entry.get("message") or "").split())
    if message:
        states = []
        if entry.get("claimed_at"):
            states.append("delivered")
        if entry.get("acknowledged_at"):
            states.append("acknowledged")
        if entry.get("superseded_at"):
            states.append("superseded")
        state_text = f" ({', '.join(states)})" if states else ""
        id_text = f" id={event_id}" if event_id else ""
        return f"-{id_text} {at} {source} {mode}{state_text}: {_clip_text(message, 420)}"
    return ""


def _lessons_for_prompt(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
    if not lessons:
        return "No durable lessons yet."
    lines = []
    for entry in lessons[-5:]:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category") or "memory")
        lesson = " ".join(str(entry.get("lesson") or "").split())
        if lesson:
            lines.append(f"- {category}: {_clip_text(lesson, SECTION_ITEM_CHARS)}")
    return "\n".join(lines) if lines else "No durable lessons yet."


def _roadmap_for_prompt(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    if not roadmap:
        return (
            "No roadmap yet. If the objective is broad, multi-phase, or needs validation checkpoints, "
            "use record_roadmap to define compact milestones, features, acceptance criteria, and validation evidence."
        )
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    status_counts: dict[str, int] = {}
    validation_counts: dict[str, int] = {}
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        status = str(milestone.get("status") or "planned")
        validation_status = str(milestone.get("validation_status") or "not_started")
        status_counts[status] = status_counts.get(status, 0) + 1
        validation_counts[validation_status] = validation_counts.get(validation_status, 0) + 1
    lines = [
        _clip_text(
            f"{roadmap.get('status') or 'planned'}: {roadmap.get('title') or 'Roadmap'}"
            + (f" | current={roadmap.get('current_milestone')}" if roadmap.get("current_milestone") else ""),
            520,
        ),
        "Milestone counts: " + (", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())) or "none"),
        "Validation counts: " + (", ".join(f"{key}={value}" for key, value in sorted(validation_counts.items())) or "none"),
    ]
    if roadmap.get("scope"):
        lines.append("Scope: " + _clip_text(str(roadmap.get("scope") or ""), 420))
    if roadmap.get("validation_contract"):
        lines.append("Validation contract: " + _clip_text(str(roadmap.get("validation_contract") or ""), 520))
    selected = [
        milestone for milestone in milestones
        if isinstance(milestone, dict)
        and str(milestone.get("status") or "planned") in {"active", "validating", "planned", "blocked"}
    ][:6]
    if not selected:
        selected = [milestone for milestone in milestones if isinstance(milestone, dict)][-4:]
    for milestone in selected[:6]:
        features = milestone.get("features") if isinstance(milestone.get("features"), list) else []
        open_features = sum(1 for feature in features if isinstance(feature, dict) and str(feature.get("status") or "planned") in {"planned", "active"})
        detail = " | ".join(
            bit
            for bit in [
                str(milestone.get("status") or "planned"),
                f"validation={milestone.get('validation_status') or 'not_started'}",
                f"p={milestone.get('priority') or 0}",
                str(milestone.get("title") or "milestone"),
                f"features={len(features)}/{open_features} open" if features else "",
            ]
            if bit
        )
        if milestone.get("acceptance_criteria"):
            detail += f" | accept={milestone.get('acceptance_criteria')}"
        if milestone.get("evidence_needed"):
            detail += f" | evidence={milestone.get('evidence_needed')}"
        if milestone.get("validation_result"):
            detail += f" | validation_result={milestone.get('validation_result')}"
        if milestone.get("next_action"):
            detail += f" | next={milestone.get('next_action')}"
        lines.append("- " + _clip_text(detail, 620))
    return "\n".join(lines)


def _metadata_list(job: dict[str, Any], key: str) -> list[dict[str, Any]]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    values = metadata.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _tasks_for_prompt(job: dict[str, Any]) -> str:
    tasks = _metadata_list(job, "task_queue")
    if not tasks:
        return (
            "No durable task queue yet. If the objective is broad, use record_tasks "
            "to create a few concrete open branches with output contracts and acceptance criteria before continuing."
        )
    status_rank = {"active": 0, "open": 1, "blocked": 2, "done": 3, "skipped": 4}
    ranked = sorted(
        tasks,
        key=lambda task: (status_rank.get(str(task.get("status") or "open"), 9), -_as_int(task.get("priority"))),
    )
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "open")
        counts[status] = counts.get(status, 0) + 1
    lines = ["Task counts: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))]
    selected = [task for task in ranked if str(task.get("status") or "open") in {"active", "open"}][:6]
    if len(selected) < 6:
        selected.extend([task for task in ranked if str(task.get("status") or "open") == "blocked"][: 6 - len(selected)])
    if len(selected) < 6:
        selected.extend([task for task in ranked if task not in selected][: 6 - len(selected)])
    for task in selected[:6]:
        bits = [
            str(task.get("status") or "open"),
            f"priority={task.get('priority') or 0}",
            str(task.get("title") or "untitled"),
        ]
        if task.get("output_contract"):
            bits.append(f"contract={task.get('output_contract')}")
        detail = " | ".join(bit for bit in bits if bit)
        if task.get("goal"):
            detail += f" | goal={task.get('goal')}"
        if task.get("acceptance_criteria"):
            detail += f" | accept={task.get('acceptance_criteria')}"
        if task.get("evidence_needed"):
            detail += f" | evidence={task.get('evidence_needed')}"
        if task.get("stall_behavior"):
            detail += f" | stall={task.get('stall_behavior')}"
        if task.get("source_hint"):
            detail += f" | source_hint={task.get('source_hint')}"
        if task.get("result"):
            detail += f" | result={task.get('result')}"
        lines.append("- " + _clip_text(detail, 520))
    return "\n".join(lines)


def _timeline_for_prompt(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No timeline events yet."
    selected: list[tuple[str, str, str]] = []
    counts: dict[str, int] = {}
    for event in events:
        rendered = _timeline_event_for_prompt(event)
        if not rendered:
            continue
        at, event_type, detail = rendered
        counts[event_type] = counts.get(event_type, 0) + 1
        selected.append((at, event_type, detail))
    if not selected:
        return "No high-signal timeline events yet. Recent state covers raw tool activity."
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    lines = [f"High-signal timeline counts: {summary}"]
    for at, event_type, detail in selected[-TIMELINE_PROMPT_EVENTS:]:
        prefix = f"- {at} {event_type}: " if at else f"- {event_type}: "
        lines.append(prefix + _clip_text(detail, SECTION_ITEM_CHARS))
    return "\n".join(lines)


def _timeline_event_for_prompt(event: dict[str, Any]) -> tuple[str, str, str] | None:
    event_type = str(event.get("event_type") or "event")
    if event_type == "operator_message":
        return None
    title = " ".join(str(event.get("title") or "").split())
    body = " ".join(str(event.get("body") or "").split())
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    title_lower = title.lower()
    if event_type == "tool_result":
        status = str(metadata.get("status") or "").lower()
        if status not in TIMELINE_PROMPT_TOOL_STATUSES:
            return None
    elif event_type == "agent_message":
        if title_lower not in TIMELINE_PROMPT_AGENT_TITLES:
            return None
    elif event_type not in TIMELINE_PROMPT_EVENT_TYPES:
        return None
    at = str(event.get("created_at") or "")
    detail = title if title else event_type
    if body:
        detail = f"{detail}: {body}"
    if event_type == "tool_result":
        status = str(metadata.get("status") or "").lower()
        detail = f"{status} {detail}".strip()
    return at, event_type, detail


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ledgers_for_prompt(job: dict[str, Any]) -> str:
    findings = _metadata_list(job, "finding_ledger")
    sources = _metadata_list(job, "source_ledger")
    lines = [
        f"Finding ledger: {len(findings)} unique candidates.",
        f"Source ledger: {len(sources)} scored sources.",
    ]
    if findings:
        lines.append("Recent findings:")
        for finding in findings[-5:]:
            bits = [
                str(finding.get("name") or "unknown"),
                str(finding.get("category") or "").strip(),
                str(finding.get("location") or "").strip(),
                f"score={finding.get('score')}" if finding.get("score") is not None else "",
            ]
            lines.append("- " + _clip_text(" | ".join(bit for bit in bits if bit), 360))
    if sources:
        usable_sources = [
            source
            for source in sources
            if _as_float(source.get("usefulness_score")) >= 0.2
            or _as_int(source.get("yield_count")) > 0
        ]
        low_quality_sources = [
            source
            for source in sources
            if _as_float(source.get("usefulness_score")) < 0.2
            and _as_int(source.get("yield_count")) <= 0
            and (_as_int(source.get("fail_count")) > 0 or source.get("warnings"))
        ]
        ranked = sorted(
            usable_sources,
            key=lambda item: (_as_float(item.get("usefulness_score")), _as_int(item.get("yield_count"))),
            reverse=True,
        )
        if ranked:
            lines.append("High-yield/current sources:")
            for source in ranked[:4]:
                lines.append(
                    "- "
                    + _clip_text(
                        f"{source.get('source')} type={source.get('source_type') or 'unknown'} "
                        f"score={source.get('usefulness_score')} findings={source.get('yield_count') or 0} "
                        f"fails={source.get('fail_count') or 0} outcome={source.get('last_outcome') or ''}",
                        420,
                    )
                )
        if low_quality_sources:
            lines.append("Low-yield/blocked source patterns to avoid:")
            for source in low_quality_sources[-3:]:
                lines.append(
                    "- "
                    + _clip_text(
                        f"{source.get('source')} type={source.get('source_type') or 'unknown'} "
                        f"score={source.get('usefulness_score')} fails={source.get('fail_count') or 0} "
                        f"warnings={', '.join(source.get('warnings') or [])} outcome={source.get('last_outcome') or ''}",
                        420,
                    )
                )
    return "\n".join(lines)


def _experiments_for_prompt(job: dict[str, Any]) -> str:
    experiments = _metadata_list(job, "experiment_ledger")
    if not experiments:
        return (
            "No experiments tracked yet. If this objective involves improving, "
            "comparing, benchmarking, reducing, increasing, or otherwise measuring something, "
            "turn candidate ideas into record_experiment entries with exact config, metric, result, and next action."
        )
    measured = [experiment for experiment in experiments if experiment.get("metric_value") is not None]
    best = [
        experiment
        for experiment in measured
        if bool(experiment.get("best_observed"))
    ]
    status_counts: dict[str, int] = {}
    for experiment in experiments:
        status = str(experiment.get("status") or "planned")
        status_counts[status] = status_counts.get(status, 0) + 1
    lines = [
        f"Experiment counts: {', '.join(f'{key}={value}' for key, value in sorted(status_counts.items()))}.",
        f"Measured results: {len(measured)}.",
    ]
    if best:
        lines.append("Best observed results:")
        for experiment in best[-3:]:
            metric = format_metric_value(
                experiment.get("metric_name") or "metric",
                experiment.get("metric_value"),
                experiment.get("metric_unit") or "",
            )
            lines.append(
                "- "
                + _clip_text(" | ".join(
                    bit
                    for bit in [
                        str(experiment.get("title") or "experiment"),
                        metric,
                        f"result={experiment.get('result')}" if experiment.get("result") else "",
                        f"next={experiment.get('next_action')}" if experiment.get("next_action") else "",
                    ]
                    if bit
                ), 520)
            )
    recent = experiments[-4:]
    if recent:
        lines.append("Recent experiments:")
        for experiment in recent:
            metric = ""
            if experiment.get("metric_value") is not None:
                metric = format_metric_value(
                    experiment.get("metric_name") or "metric",
                    experiment.get("metric_value"),
                    experiment.get("metric_unit") or "",
                )
            delta = ""
            if experiment.get("delta_from_previous_best") is not None:
                delta = f"delta={experiment.get('delta_from_previous_best')}"
            lines.append(
                "- "
                + _clip_text(" | ".join(
                    bit
                    for bit in [
                        str(experiment.get("status") or "planned"),
                        str(experiment.get("title") or "experiment"),
                        metric,
                        delta,
                        f"next={experiment.get('next_action')}" if experiment.get("next_action") else "",
                    ]
                    if bit
                ), 520)
            )
    return "\n".join(lines)


def _measured_progress_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _measured_progress_guard_context(job, recent_steps)
    if not context:
        return "None."
    if _as_int(context.get("shell_actions_since_last_experiment")) >= _as_int(context.get("shell_action_budget")):
        return (
            "This objective or active task is measurably framed, and the shell/action budget since the last experiment "
            f"is exhausted. completed_since_last_experiment={context.get('completed_since_last_experiment')} "
            f"shell_actions={context.get('shell_actions_since_last_experiment')} shell_budget={context.get('shell_action_budget')} "
            f"reason={context.get('reason')}. Do not call shell_exec or do more research next. Use record_experiment "
            "for a known result, record_tasks to create a missing experiment/monitor branch, or record_lesson if the "
            "branch is blocked or the recent outputs were not valid measurements."
        )
    return (
        "This objective or active task is measurably framed, but recent work has not produced "
        f"new experiment records. completed_since_last_experiment={context.get('completed_since_last_experiment')} "
        f"research_budget={context.get('research_budget')} shell_actions={context.get('shell_actions_since_last_experiment')} "
        f"shell_budget={context.get('shell_action_budget')} reason={context.get('reason')}. "
        "Next useful actions: run a small measuring action, call record_experiment for a known result, "
        "or use record_tasks to create an experiment/action/monitor task with acceptance criteria and evidence."
    )


def _measurement_obligation_for_prompt(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    obligation = metadata.get("pending_measurement_obligation")
    if not isinstance(obligation, dict) or not obligation or obligation.get("resolved_at"):
        return "None."
    candidates = obligation.get("metric_candidates") if isinstance(obligation.get("metric_candidates"), list) else []
    lines = [
        f"source_step=#{obligation.get('source_step_no') or '?'} tool={obligation.get('tool') or ''}",
        f"summary={obligation.get('summary') or ''}",
    ]
    command = str(obligation.get("command") or "")
    if command:
        lines.append(f"command={_clip_text(command, 360)}")
    if candidates:
        lines.append("metric_candidates=" + "; ".join(str(item) for item in candidates[:6]))
    lines.append(
        "Before more research or artifact churn, call record_experiment with the measured result, "
        "record_lesson explaining why it is not a valid measurement, or record_tasks to create the missing measurement branch."
    )
    return "\n".join(lines)


def _progress_accounting_for_prompt(recent_steps: list[dict[str, Any]]) -> str:
    context = _artifact_accounting_context(recent_steps)
    if not context:
        return "None."
    return (
        "Recent saved outputs need accounting before more output/research. "
        f"artifact_count={context.get('artifact_count')} since_step={context.get('since_step')} "
        f"artifact_titles={'; '.join(str(title) for title in context.get('artifact_titles', [])[:4])}. "
        "Next use record_tasks or record_roadmap to mark progress/reopen branches, "
        "record_findings or record_source for reusable evidence, record_experiment for measured results, "
        "record_milestone_validation for milestone checks, or record_lesson if these outputs are not useful."
    )


def _activity_stagnation_for_prompt(job: dict[str, Any]) -> str:
    context = _activity_stagnation_context(job)
    if not context:
        return "None."
    return (
        "Recent checkpoints have reported activity without durable progress. "
        f"activity_checkpoint_streak={context.get('streak')} threshold={context.get('threshold')} "
        f"last_counts={context.get('counts')}. "
        "Next classify the branch with record_findings, record_source, record_experiment, record_tasks, "
        "record_roadmap, record_milestone_validation, or record_lesson. If the branch is low-yield, mark it "
        "blocked/skipped and pivot before doing more read-only work or saving more outputs."
    )


def _reflections_for_prompt(job: dict[str, Any]) -> str:
    reflections = _metadata_list(job, "reflections")
    if not reflections:
        return "No reflection checkpoints yet."
    lines = []
    for reflection in reflections[-2:]:
        strategy = f" strategy={reflection.get('strategy')}" if reflection.get("strategy") else ""
        lines.append("- " + _clip_text(f"{reflection.get('summary')}{strategy}", 520))
    return "\n".join(lines)


def _next_action_constraint(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    measurement_obligation = _pending_measurement_obligation(job)
    if measurement_obligation:
        return (
            "A pending measurement obligation is active from "
            f"step #{measurement_obligation.get('source_step_no') or '?'}. "
            "Resolve it with record_experiment, record_lesson explaining why it is invalid, "
            "or record_tasks creating the missing measurement branch before more research/artifact churn."
        )
    artifact_accounting = _artifact_accounting_context(recent_steps)
    if artifact_accounting:
        return (
            "Recent saved outputs need durable accounting. Before more artifact writing, reading, research, browsing, "
            "or shell work, use record_tasks, record_roadmap, record_milestone_validation, record_findings, record_source, record_experiment, or record_lesson "
            "to explain what changed and what branch is next."
        )
    measured_guard = _measured_progress_guard_context(job, recent_steps)
    if measured_guard:
        return (
            "This job needs measured progress, not more research-only activity. "
            "Do one of: run a small measuring command/action, call record_experiment for a known measurement, "
            "record_tasks with an experiment/action/monitor contract, or record_lesson if measurement is blocked."
        )
    activity_stagnation = _activity_stagnation_context(job)
    if activity_stagnation:
        return (
            "Recent checkpoints show activity without durable progress. "
            "Use a ledger or planning tool to classify what changed, reject the low-yield branch, or open a better branch "
            "before more read-only work or output churn."
        )
    experiment_next_action = _latest_experiment_next_action_context(job)
    if experiment_next_action:
        return (
            "The latest measured experiment selected a concrete next action. "
            f"Next action: {_clip_text(experiment_next_action.get('next_action') or '', 520)}. "
            "Act on it with the appropriate tool, or use record_tasks/record_lesson if it is invalid or blocked. "
            "Do not bury it under more checkpoints or unrelated research."
        )
    milestone_validation = _milestone_validation_needed(job)
    if milestone_validation:
        return (
            f"Roadmap milestone '{milestone_validation.get('title')}' is ready for validation or is marked validating. "
            "Use record_milestone_validation with evidence and pass/fail/blocker status, then create follow-up tasks for gaps."
        )
    roadmap_staleness = _roadmap_staleness_context(job, recent_steps)
    if roadmap_staleness:
        return (
            "The roadmap has not advanced despite durable task/artifact activity. "
            "Use record_roadmap to mark the current milestone active/done/blocked, or record_milestone_validation "
            "if acceptance criteria can be judged from existing evidence, before more branch work."
        )
    if _roadmap_missing_for_broad_job(job):
        return (
            "The objective is broad enough to benefit from roadmap control. Use record_roadmap to define compact milestones, "
            "features, acceptance criteria, and validation checkpoints before expanding the task queue further."
        )
    evidence_step = _unpersisted_evidence_step(recent_steps)
    if evidence_step:
        return (
            f"You have unsaved evidence from step #{evidence_step['step_no']} "
            f"({evidence_step.get('tool_name') or evidence_step['kind']}). "
            "Your next tool call should usually be write_artifact. If this evidence taught a durable rule, record_lesson after saving it."
        )
    if _task_queue_exhausted(job):
        return (
            "All durable task branches are done, skipped, or blocked. Before more research or execution, "
            "use record_tasks to open the next concrete branch, or report_update if the operator needs a checkpoint."
        )
    for step in reversed(recent_steps[-5:]):
        error = str(step.get("error") or "")
        if error == "artifact required before more research":
            return "The last blocked action needs write_artifact, not another search or browser action."
        if error == "task branch required before more work":
            return "Create or reopen a task branch with record_tasks before doing more research or execution."
        if error in {"duplicate tool call blocked", "similar search query blocked", "search loop blocked"}:
            output = step.get("output") if isinstance(step.get("output"), dict) else {}
            blocked_tool = str(output.get("blocked_tool") or "")
            if blocked_tool == "read_artifact":
                return "Do not read the same artifact again. Use its content to choose a concrete next action: inspect a specific item, record findings/tasks, or write a report artifact."
            if blocked_tool == "shell_exec":
                return "Do not rerun the same shell discovery command. Use the prior output to inspect a specific file/item, save it, or update findings/tasks."
            return "Change source, extract an existing result, save an artifact, or record a lesson about the failed strategy."
    return "No special constraint beyond taking one bounded useful action."


def _milestone_validation_needed(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        status = str(milestone.get("status") or "planned")
        validation_status = str(milestone.get("validation_status") or "not_started")
        if status == "validating" or validation_status == "pending":
            return milestone
        features = milestone.get("features") if isinstance(milestone.get("features"), list) else []
        if status == "active" and features and all(
            isinstance(feature, dict) and str(feature.get("status") or "planned") in {"done", "skipped"}
            for feature in features
        ):
            return milestone
    return None


def _latest_experiment_next_action_context(job: dict[str, Any]) -> dict[str, Any] | None:
    experiments = _metadata_list(job, "experiment_ledger")
    for experiment in reversed(experiments):
        if not isinstance(experiment, dict):
            continue
        status = str(experiment.get("status") or "").strip().lower()
        next_action = str(experiment.get("next_action") or "").strip()
        if not next_action:
            continue
        if status in {"measured", "failed", "blocked"} or experiment.get("metric_value") is not None:
            return {
                "title": experiment.get("title"),
                "status": status,
                "metric_name": experiment.get("metric_name"),
                "metric_value": experiment.get("metric_value"),
                "next_action": next_action,
            }
    return None


def _experiment_next_action_requires_delivery(context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    next_action = str(context.get("next_action") or "").lower()
    if not next_action:
        return False
    tokens = set(re.findall(r"[a-z][a-z0-9_-]+", next_action))
    if not tokens & EXPERIMENT_DELIVERY_ACTION_TERMS:
        return False
    return not bool(tokens & EXPERIMENT_INFORMATION_ACTION_TERMS)


def _shell_command_looks_like_write(command: str) -> bool:
    text = command.strip()
    if not text:
        return False
    write_patterns = [
        r"(?<!\d)>>?\s*[^&]",
        r"\b1>>?\s*[^&]",
        r"\btee\b",
        r"\bcat\s+>\b",
        r"\bpython[0-9.]*\b.*\bwrite_text\b",
        r"\bpython[0-9.]*\b.*\bopen\([^)]*,\s*['\"]w",
        r"\bsed\s+-i\b",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in write_patterns)


def _shell_command_looks_read_only(command: str) -> bool:
    if not command.strip():
        return False
    if _shell_command_looks_like_write(command):
        return False
    return bool(READ_ONLY_SHELL_COMMAND_PATTERN.search(command))


def _roadmap_staleness_context(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    if not milestones:
        return None
    if any(step.get("tool_name") in {"record_roadmap", "record_milestone_validation"} for step in recent_steps):
        return None
    if any(
        isinstance(milestone, dict)
        and (
            str(milestone.get("status") or "planned") != "planned"
            or str(milestone.get("validation_status") or "not_started") != "not_started"
        )
        for milestone in milestones
    ):
        return None
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    completed_artifacts = [
        step for step in recent_steps
        if step.get("status") == "completed" and step.get("tool_name") == "write_artifact"
    ]
    task_updates = [
        step for step in recent_steps
        if step.get("status") == "completed" and step.get("tool_name") == "record_tasks"
    ]
    if len(completed_artifacts) < 2 and len(task_updates) < 2 and len(tasks) < 8:
        return None
    return {
        "title": roadmap.get("title") or "Roadmap",
        "status": roadmap.get("status") or "planned",
        "milestone_count": len(milestones),
        "task_count": len(tasks),
        "artifact_count": len(completed_artifacts),
        "task_update_count": len(task_updates),
    }


def _roadmap_missing_for_broad_job(job: dict[str, Any]) -> bool:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    if isinstance(metadata.get("roadmap"), dict):
        return False
    objective = str(job.get("objective") or "")
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    if len(tasks) >= 6:
        return True
    words = re.findall(r"[A-Za-z0-9_]+", objective)
    broad_terms = {"build", "create", "develop", "implement", "research", "improve", "optimize", "migrate", "write", "analyze"}
    return len(words) >= 14 and any(term in objective.lower() for term in broad_terms)


def _task_queue_exhausted(job: dict[str, Any]) -> bool:
    tasks = _metadata_list(job, "task_queue")
    if not tasks:
        return False
    runnable = {"open", "active"}
    return not any(str(task.get("status") or "open").strip().lower() in runnable for task in tasks)


def _task_queue_saturation_context(job: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    tasks = _metadata_list(job, "task_queue")
    open_tasks = [task for task in tasks if str(task.get("status") or "open").strip().lower() in {"open", "active"}]
    if len(open_tasks) < TASK_QUEUE_SATURATION_OPEN_TASKS:
        return None
    incoming = args.get("tasks") if isinstance(args.get("tasks"), list) else []
    if not incoming:
        return None
    existing_keys = {
        _norm_task_key(str(task.get("parent") or ""), str(task.get("title") or ""))
        for task in tasks
    }
    new_open_titles = []
    for task in incoming:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "open").strip().lower().replace(" ", "_")
        if status not in {"open", "active"}:
            continue
        key = _norm_task_key(str(task.get("parent") or ""), str(task.get("title") or ""))
        if key not in existing_keys:
            new_open_titles.append(str(task.get("title") or "").strip())
    if not new_open_titles:
        return None
    return {
        "open_count": len(open_tasks),
        "threshold": TASK_QUEUE_SATURATION_OPEN_TASKS,
        "new_open_count": len(new_open_titles),
        "new_open_titles": new_open_titles[:8],
    }


def _norm_task_key(parent: str, title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{parent}|{title}".lower()).strip("-")


def _parse_tool_result(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except json.JSONDecodeError:
        return {"result": raw}


def _load_program_text(config: AppConfig, job_id: str) -> str:
    path = config.runtime.jobs_dir / job_id / "program.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _browser_warning_context(output: dict[str, Any]) -> dict[str, str] | None:
    data = output.get("data") if isinstance(output.get("data"), dict) else {}
    title = str(data.get("title") or "")
    url = str(data.get("url") or data.get("origin") or output.get("url") or "")
    snapshot = str(output.get("snapshot") or data.get("snapshot") or output.get("data") or "")
    reason = anti_bot_reason(title, url, snapshot)
    if not reason:
        return None
    return {"reason": reason, "url": url, "title": title}


def _recent_anti_bot_context(recent_steps: list[dict[str, Any]], *, window: int = 8) -> dict[str, Any] | None:
    for step in reversed(recent_steps[-window:]):
        if step.get("status") != "completed" or step.get("tool_name") not in {"browser_navigate", "browser_snapshot"}:
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        warning = _browser_warning_context(output)
        if warning:
            return {**warning, "step_id": step.get("id"), "step_no": step.get("step_no")}
    return None


def _artifact_args_acknowledge_block(args: dict[str, Any]) -> bool:
    text = " ".join(str(args.get(key) or "") for key in ("title", "summary", "content")).lower()
    return any(term in text for term in ANTI_BOT_ACK_TERMS)


def _same_source_url(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left.split("#", 1)[0].rstrip("/") == right.split("#", 1)[0].rstrip("/")


def _normalized_source_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        return f"https://{value}"
    return value


def _source_host(value: str) -> str:
    parsed = urlparse(_normalized_source_url(value))
    return parsed.netloc.lower().removeprefix("www.")


def _source_matches(left: str, right: str) -> bool:
    if _same_source_url(left, right):
        return True
    left_host = _source_host(left)
    right_host = _source_host(right)
    return bool(left_host and right_host and left_host == right_host)


def _known_bad_sources(job: dict[str, Any]) -> list[dict[str, Any]]:
    bad_sources = []
    for source in _metadata_list(job, "source_ledger"):
        if (
            _as_float(source.get("usefulness_score")) < 0.2
            and _as_int(source.get("yield_count")) <= 0
            and (_as_int(source.get("fail_count")) > 0 or source.get("warnings"))
        ):
            bad_sources.append(source)
    return bad_sources


def _known_bad_source_for_call(name: str, args: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None:
    if name not in {"browser_navigate", "web_extract"}:
        return None
    bad_sources = _known_bad_sources(job)
    if not bad_sources:
        return None
    urls: list[str] = []
    if name == "browser_navigate":
        urls = [str(args.get("url") or "")]
    elif isinstance(args.get("urls"), list):
        urls = [str(url) for url in args["urls"]]
    for url in [url for url in urls if url.strip()]:
        for source in bad_sources:
            source_value = str(source.get("source") or "")
            if source_value and _source_matches(url, source_value):
                return source
    return None


def _tool_signature(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"


def _duplicate_recent_tool_call(
    name: str,
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 24,
) -> dict[str, Any] | None:
    if name in {"browser_snapshot", "defer_job"}:
        return None
    signature = _tool_signature(name, args)
    for step in reversed(recent_steps[-window:]):
        if step.get("status") != "completed" or step.get("tool_name") != name:
            continue
        input_data = step.get("input") or {}
        previous_args = input_data.get("arguments") if isinstance(input_data, dict) else None
        if isinstance(previous_args, dict) and _tool_signature(name, previous_args) == signature:
            return step
    return None


def _completed_recent_steps(recent_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in recent_steps if step.get("status") == "completed"]


def _step_has_evidence(step: dict[str, Any]) -> bool:
    tool_name = step.get("tool_name")
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    if tool_name == "web_extract":
        pages = output.get("pages") if isinstance(output.get("pages"), list) else []
        for page in pages:
            if page.get("error"):
                continue
            if str(page.get("text") or "").strip():
                return True
    if tool_name in {"browser_navigate", "browser_snapshot"}:
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        snapshot = str(output.get("snapshot") or data.get("snapshot") or "")
        if anti_bot_reason(str(data.get("title") or ""), str(data.get("url") or data.get("origin") or ""), snapshot):
            return False
        return len(snapshot.strip()) >= 500
    if tool_name == "shell_exec":
        text = "\n".join(str(output.get(key) or "") for key in ("stdout", "stderr"))
        return len(text.strip()) >= 1000
    return False


def _unpersisted_evidence_step(recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    for step in reversed(recent_steps):
        if step.get("status") not in {"completed", "blocked"}:
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        if step.get("tool_name") == "write_artifact":
            return None
        if isinstance(output.get("auto_checkpoint"), dict):
            return None
        if step.get("status") == "completed" and _step_has_evidence(step):
            return step
    return None


def _recent_search_streak(recent_steps: list[dict[str, Any]]) -> int:
    return _recent_tool_streak(recent_steps, "web_search")


def _pending_measurement_obligation(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    obligation = metadata.get("pending_measurement_obligation")
    if isinstance(obligation, dict) and obligation and not obligation.get("resolved_at"):
        return obligation
    return None


def _clear_invalid_measurement_obligation(db: AgentDB, job_id: str) -> bool:
    job = db.get_job(job_id)
    obligation = _pending_measurement_obligation(job)
    if not obligation:
        return False
    candidates = obligation.get("metric_candidates") if isinstance(obligation.get("metric_candidates"), list) else []
    if not candidates:
        return False
    command = str(obligation.get("command") or "")
    if not measurement_candidates_are_diagnostic_only(candidates, command=command):
        return False
    db.update_job_metadata(job_id, {"pending_measurement_obligation": {}})
    db.append_agent_update(
        job_id,
        "Cleared measurement obligation because the output was diagnostic context, not a trial result.",
        category="progress",
        metadata={"cleared_measurement_obligation": obligation},
    )
    return True


def _progress_churn_context(recent_steps: list[dict[str, Any]], *, window: int = 10) -> dict[str, Any] | None:
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    tail = completed[-window:]
    if len(tail) < 8:
        return None
    if any(step.get("tool_name") in LEDGER_PROGRESS_TOOLS for step in tail):
        return None
    churn_count = sum(1 for step in tail if step.get("tool_name") in CHURN_TOOLS)
    if churn_count < 7:
        return None
    return {
        "window": len(tail),
        "churn_count": churn_count,
        "since_step": tail[0].get("step_no"),
        "tools": [step.get("tool_name") or step.get("kind") for step in tail],
    }


def _activity_stagnation_context(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    streak = _as_int(metadata.get("activity_checkpoint_streak"))
    if streak < ACTIVITY_STAGNATION_CHECKPOINTS:
        return None
    counts = metadata.get("last_checkpoint_counts") if isinstance(metadata.get("last_checkpoint_counts"), dict) else {}
    return {
        "streak": streak,
        "threshold": ACTIVITY_STAGNATION_CHECKPOINTS,
        "counts": {key: _as_int(counts.get(key)) for key in ("findings", "sources", "tasks", "experiments", "lessons", "milestones")},
    }


def _artifact_accounting_context(
    recent_steps: list[dict[str, Any]],
    *,
    threshold: int = 3,
    window: int = 12,
) -> dict[str, Any] | None:
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    tail: list[dict[str, Any]] = []
    for step in reversed(completed[-window:]):
        if step.get("tool_name") in LEDGER_PROGRESS_TOOLS:
            break
        tail.append(step)
    tail.reverse()
    artifact_steps = [step for step in tail if step.get("tool_name") == "write_artifact"]
    if len(artifact_steps) < threshold:
        return None
    titles = []
    for step in artifact_steps[-5:]:
        input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
        args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
        title = str(args.get("title") or step.get("summary") or f"step #{step.get('step_no')}")
        titles.append(_clip_text(title, 120))
    return {
        "artifact_count": len(artifact_steps),
        "since_step": tail[0].get("step_no") if tail else None,
        "artifact_steps": [step.get("step_no") for step in artifact_steps],
        "artifact_titles": titles,
        "tools": [step.get("tool_name") or step.get("kind") for step in tail],
    }


def _job_requires_measured_progress(job: dict[str, Any]) -> bool:
    text_parts = [
        str(job.get("title") or ""),
        str(job.get("objective") or ""),
        str(job.get("kind") or ""),
    ]
    tasks = _metadata_list(job, "task_queue")
    for task in tasks:
        status = str(task.get("status") or "open")
        if status in {"done", "skipped"}:
            continue
        contract = str(task.get("output_contract") or "")
        if contract in {"experiment", "monitor"}:
            return True
        if contract == "action" and _task_text_requires_measurement(task):
            return True
        text_parts.extend(
            str(task.get(key) or "")
            for key in ("title", "goal", "acceptance_criteria", "evidence_needed", "stall_behavior")
        )
    return any(MEASURABLE_PROGRESS_PATTERN.search(part) for part in text_parts if part)


def _task_text_requires_measurement(task: dict[str, Any]) -> bool:
    return any(
        MEASURABLE_PROGRESS_PATTERN.search(str(task.get(key) or ""))
        for key in ("title", "goal", "acceptance_criteria", "evidence_needed", "stall_behavior")
    )


def _measured_progress_guard_context(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    budget: int = MEASURABLE_RESEARCH_BUDGET_STEPS,
) -> dict[str, Any] | None:
    if not _job_requires_measured_progress(job):
        return None
    if _pending_measurement_obligation(job):
        return None
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    if not completed:
        return None
    last_experiment_index = -1
    for index, step in enumerate(completed):
        if step.get("tool_name") == "record_experiment":
            last_experiment_index = index
    tail = completed[last_experiment_index + 1 :]
    branch_activity = [step for step in tail if step.get("tool_name") in BRANCH_WORK_TOOLS | {"write_artifact"}]
    shell_actions = [step for step in tail if step.get("tool_name") == "shell_exec"]
    if len(branch_activity) < budget and len(shell_actions) < MEASURABLE_ACTION_BUDGET_STEPS:
        return None
    if any(step.get("tool_name") in {"record_tasks", "record_lesson"} for step in tail[-6:]):
        return None
    experiments = _metadata_list(job, "experiment_ledger")
    reason = "no experiment records yet" if not experiments else "no recent experiment update"
    return {
        "reason": reason,
        "research_budget": budget,
        "shell_action_budget": MEASURABLE_ACTION_BUDGET_STEPS,
        "completed_since_last_experiment": len(tail),
        "branch_activity": len(branch_activity),
        "shell_actions_since_last_experiment": len(shell_actions),
        "since_step": branch_activity[0].get("step_no") if branch_activity else None,
        "tools": [step.get("tool_name") or step.get("kind") for step in branch_activity[-10:]],
    }


def _maybe_create_measurement_obligation(
    *,
    db: AgentDB,
    job_id: str,
    step: dict[str, Any] | None,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if tool_name != "shell_exec":
        return
    command = str(args.get("command") or result.get("command") or "")
    candidates = measurement_candidates(result, command=command)
    if not candidates:
        return
    metadata = db.get_job(job_id).get("metadata")
    if isinstance(metadata, dict):
        existing = metadata.get("pending_measurement_obligation")
        if isinstance(existing, dict) and existing and not existing.get("resolved_at"):
            return
    obligation = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_step_id": step.get("id") if step else "",
        "source_step_no": step.get("step_no") if step else None,
        "tool": tool_name,
        "summary": "Tool output contains measurable-looking results that need experiment accounting.",
        "metric_candidates": candidates,
        "command": command[:1000],
    }
    db.update_job_metadata(job_id, {"pending_measurement_obligation": obligation})
    db.append_agent_update(
        job_id,
        f"Measured output needs accounting: {', '.join(candidates[:3])}.",
        category="blocked",
        metadata={"pending_measurement_obligation": obligation},
    )


def _step_by_id(db: AgentDB, job_id: str, step_id: str) -> dict[str, Any] | None:
    for step in db.list_steps(job_id=job_id):
        if str(step.get("id") or "") == step_id:
            return step
    return None


def _search_query(args: dict[str, Any]) -> str:
    return str(args.get("query") or "").strip()


def _query_tokens(query: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", query.lower())
        if len(token) > 2 and token not in QUERY_STOPWORDS
    }


def _text_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 2 and token not in TEXT_TOKEN_STOPWORDS
    }


def _similar_recent_search(
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 12,
) -> dict[str, Any] | None:
    return _similar_recent_query_tool("web_search", args, recent_steps, window=window)


def _similar_recent_query_tool(
    tool_name: str,
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 12,
) -> dict[str, Any] | None:
    query = _search_query(args)
    tokens = _query_tokens(query)
    if len(tokens) < 2:
        return None
    for step in reversed(_completed_recent_steps(recent_steps)[-window:]):
        if step.get("tool_name") != tool_name:
            continue
        input_data = step.get("input") or {}
        previous_args = input_data.get("arguments") if isinstance(input_data, dict) else None
        if not isinstance(previous_args, dict):
            continue
        previous_query = _search_query(previous_args)
        previous_tokens = _query_tokens(previous_query)
        if len(previous_tokens) < 2:
            continue
        overlap = len(tokens & previous_tokens) / max(len(tokens), len(previous_tokens))
        if overlap >= 0.72:
            return step
    return None


def _recent_tool_streak(recent_steps: list[dict[str, Any]], tool_name: str) -> int:
    streak = 0
    for step in reversed(_completed_recent_steps(recent_steps)):
        current_tool = step.get("tool_name")
        if current_tool == tool_name:
            streak += 1
            continue
        if current_tool:
            break
    return streak


def _repeated_guard_block_context(
    recent_steps: list[dict[str, Any]],
    *,
    threshold: int = 3,
    window: int = 12,
) -> dict[str, Any] | None:
    last_recovery_no = max(
        (
            int(step.get("step_no") or 0)
            for step in recent_steps
            if step.get("tool_name") == "guard_recovery" and step.get("status") == "completed"
        ),
        default=0,
    )
    operational_steps = [
        step
        for step in recent_steps
        if int(step.get("step_no") or 0) > last_recovery_no
        if step.get("kind") in {"tool", "recovery"} and step.get("tool_name") != "guard_recovery"
    ]
    tail = operational_steps[-window:]
    latest_blocked = next((step for step in reversed(tail) if step.get("status") == "blocked"), None)
    if not latest_blocked:
        return None
    output = latest_blocked.get("output") if isinstance(latest_blocked.get("output"), dict) else {}
    error = str(output.get("error") or latest_blocked.get("error") or "")
    if error not in RECOVERABLE_GUARD_ERRORS:
        return None
    count = 0
    blocked_tools = []
    first_step_no = None
    for step in tail:
        step_output = step.get("output") if isinstance(step.get("output"), dict) else {}
        step_error = str(step_output.get("error") or step.get("error") or "")
        if step.get("status") == "blocked" and step_error == error:
            count += 1
            first_step_no = first_step_no or step.get("step_no")
            blocked_tools.append(str(step.get("tool_name") or step.get("kind") or "tool"))
    if count < threshold:
        return None
    return {
        "error": error,
        "count": count,
        "first_step_no": first_step_no,
        "latest_step_no": latest_blocked.get("step_no"),
        "blocked_tools": blocked_tools[-8:],
    }


def _blocked_tool_call_result(
    name: str,
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    job: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    if name == "record_tasks":
        saturated = _task_queue_saturation_context(job, args)
        if saturated:
            result = {
                "success": False,
                "error": "task queue saturated",
                "blocked_tool": name,
                "blocked_arguments": args,
                "task_queue": saturated,
                "guidance": (
                    "The durable task queue already has many open branches. Do not create more open tasks. "
                    "Choose an existing high-priority task and execute it, or update existing tasks to active, "
                    "done, blocked, or skipped."
                ),
            }
            return result, f"blocked record_tasks; {saturated['open_count']} open tasks already"

    duplicate_step = _duplicate_recent_tool_call(name, args, recent_steps)
    if duplicate_step:
        guidance = "Use a different query, extract one of the prior result URLs, open a result in the browser, or write an artifact."
        if name == "read_artifact":
            guidance = (
                "This artifact was already read. Do not read it again; use its content to inspect a concrete item, "
                "record findings/tasks, or write a report artifact."
            )
        elif name == "shell_exec":
            guidance = (
                "This shell command was already run. Do not rerun discovery; use the previous output to inspect a "
                "specific file/item, write an artifact, or update findings/tasks."
            )
        result = {
            "success": False,
            "error": "duplicate tool call blocked",
            "blocked_tool": name,
            "blocked_arguments": args,
            "previous_step": duplicate_step["id"],
            "guidance": guidance,
        }
        return result, f"blocked duplicate {name}; previous step #{duplicate_step['step_no']}"

    measurement_obligation = _pending_measurement_obligation(job)
    if measurement_obligation and name in MEASUREMENT_BLOCKED_TOOLS and name not in MEASUREMENT_RESOLUTION_TOOLS:
        result = {
            "success": False,
            "error": "measurement obligation pending",
            "blocked_tool": name,
            "blocked_arguments": args,
            "pending_measurement_obligation": measurement_obligation,
            "guidance": (
                "A recent action produced measurable output. Record it with record_experiment, "
                "explain why it is invalid with record_lesson, or create the missing measurement branch with record_tasks "
                "before doing more research, artifact writing, or finding/source updates."
            ),
        }
        return result, f"blocked {name}; record_experiment required after measured output"

    measured_progress_guard = _measured_progress_guard_context(job, recent_steps)
    progress_churn = _progress_churn_context(recent_steps)
    artifact_accounting = _artifact_accounting_context(recent_steps)
    activity_stagnation = _activity_stagnation_context(job)
    if (
        artifact_accounting
        and name in ARTIFACT_ACCOUNTING_BLOCKED_TOOLS
        and name not in ARTIFACT_ACCOUNTING_RESOLUTION_TOOLS
    ):
        result = {
            "success": False,
            "error": "progress accounting required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "artifact_accounting": artifact_accounting,
            "guidance": (
                "Recent saved outputs have not been reflected in durable progress state. "
                "Use record_tasks or record_roadmap to mark completed/open branches, "
                "record_milestone_validation for milestone checks, record_findings or record_source "
                "for reusable evidence, record_experiment for measurements, or record_lesson "
                "if the outputs were low-value before continuing."
            ),
        }
        return result, f"blocked {name}; progress accounting required after saved outputs"

    if progress_churn and not measured_progress_guard and name in CHURN_TOOLS:
        result = {
            "success": False,
            "error": "progress ledger update required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "progress_churn": progress_churn,
            "guidance": (
                "Recent activity has not changed findings, experiments, tasks, lessons, or sources. "
                "Use a ledger tool to record progress, reject the branch, or create a pivot task before continuing."
            ),
        }
        return result, f"blocked {name}; progress ledger update required"

    if activity_stagnation and name in ACTIVITY_STAGNATION_BLOCKED_TOOLS:
        result = {
            "success": False,
            "error": "durable progress required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "activity_stagnation": activity_stagnation,
            "guidance": (
                "Several checkpoints have produced no durable ledger delta. "
                "Use record_findings, record_source, record_experiment, record_tasks, record_roadmap, "
                "record_milestone_validation, or record_lesson to classify the branch, mark it blocked/skipped, "
                "or open a better branch before more research, shell, file, report, or artifact work."
            ),
        }
        return result, f"blocked {name}; durable progress required after activity-only checkpoints"

    roadmap_staleness = _roadmap_staleness_context(job, recent_steps)
    if roadmap_staleness and name in ROADMAP_STALENESS_BLOCKED_TOOLS:
        result = {
            "success": False,
            "error": "roadmap update required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "roadmap_staleness": roadmap_staleness,
            "guidance": (
                "The roadmap has not advanced despite durable task/artifact activity. "
                "Use record_roadmap to mark milestone progress, record_milestone_validation "
                "to judge an evidence-backed checkpoint, or record_lesson if the roadmap is wrong."
            ),
        }
        return result, f"blocked {name}; roadmap update required"

    milestone_validation = _milestone_validation_needed(job)
    if milestone_validation and name in MILESTONE_VALIDATION_BLOCKED_TOOLS:
        result = {
            "success": False,
            "error": "milestone validation required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "milestone": {
                "title": milestone_validation.get("title"),
                "status": milestone_validation.get("status"),
                "validation_status": milestone_validation.get("validation_status"),
                "acceptance_criteria": milestone_validation.get("acceptance_criteria"),
                "evidence_needed": milestone_validation.get("evidence_needed"),
            },
            "guidance": (
                "The current milestone is ready for validation. Use record_milestone_validation "
                "with evidence and pass/fail/blocker status, read an existing artifact if needed, "
                "or create follow-up tasks for validation gaps before starting more branch work."
            ),
        }
        return result, f"blocked {name}; milestone validation required"

    anti_bot_context = _recent_anti_bot_context(recent_steps)
    if anti_bot_context:
        blocked_browser_followups = {"browser_click", "browser_console", "browser_press", "browser_scroll", "browser_snapshot", "browser_type"}
        if name in blocked_browser_followups:
            result = {
                "success": False,
                "error": "anti-bot source loop blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "anti_bot_source": anti_bot_context,
                "guidance": "This page is blocked by anti-bot/CAPTCHA. Record the source as blocked and pivot to a different public source.",
            }
            return result, f"blocked {name}; anti-bot source at step #{anti_bot_context.get('step_no')}"
        if name == "browser_navigate" and _same_source_url(str(args.get("url") or ""), str(anti_bot_context.get("url") or "")):
            result = {
                "success": False,
                "error": "anti-bot source loop blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "anti_bot_source": anti_bot_context,
                "guidance": "Do not reopen the same blocked source. Pivot to another source.",
            }
            return result, f"blocked {name}; repeated blocked source from step #{anti_bot_context.get('step_no')}"
        if name == "web_extract":
            urls = args.get("urls") if isinstance(args.get("urls"), list) else []
            if any(_same_source_url(str(url), str(anti_bot_context.get("url") or "")) for url in urls):
                result = {
                    "success": False,
                    "error": "anti-bot source loop blocked",
                    "blocked_tool": name,
                    "blocked_arguments": args,
                    "anti_bot_source": anti_bot_context,
                    "guidance": "Do not extract the same blocked source. Record it as low-yield and pivot.",
                }
                return result, f"blocked {name}; blocked source from step #{anti_bot_context.get('step_no')}"
        if name == "write_artifact" and not _artifact_args_acknowledge_block(args):
            result = {
                "success": False,
                "error": "misleading blocked-source artifact blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "anti_bot_source": anti_bot_context,
                "guidance": "The latest browser evidence is an anti-bot/CAPTCHA block. Write only a blocked-source note or pivot.",
            }
            return result, f"blocked misleading write_artifact; anti-bot source at step #{anti_bot_context.get('step_no')}"

    unpersisted_evidence = _unpersisted_evidence_step(recent_steps)
    if unpersisted_evidence and name in BRANCH_WORK_TOOLS:
        result = {
            "success": False,
            "error": "artifact required before more research",
            "blocked_tool": name,
            "blocked_arguments": args,
            "previous_step": unpersisted_evidence["id"],
            "guidance": "Write an artifact summarizing the browser, extracted, or shell evidence before doing more search, browsing, or shell work.",
        }
        return result, f"blocked {name}; write_artifact required after evidence step #{unpersisted_evidence['step_no']}"

    experiment_next_action = _latest_experiment_next_action_context(job)
    if (
        _experiment_next_action_requires_delivery(experiment_next_action)
        and (
            name in EXPERIMENT_NEXT_ACTION_BLOCKED_TOOLS
            or (
                name == "shell_exec"
                and _shell_command_looks_read_only(str(args.get("command") or ""))
            )
        )
    ):
        result = {
            "success": False,
            "error": "experiment next action pending",
            "blocked_tool": name,
            "blocked_arguments": args,
            "experiment_next_action": experiment_next_action,
            "guidance": (
                "The latest measured experiment selected a delivery/action next step. "
                "Act on that next action with an execution or ledger tool, or use record_tasks/record_lesson "
                "to explain why it is invalid or blocked before doing more research or artifact review."
            ),
        }
        return result, f"blocked {name}; experiment next action pending"

    shell_budget_exhausted = (
        name == "shell_exec"
        and _as_int(measured_progress_guard.get("shell_actions_since_last_experiment")) >= MEASURABLE_ACTION_BUDGET_STEPS
    ) if measured_progress_guard else False
    if measured_progress_guard and (name in MEASURABLE_RESEARCH_BLOCKED_TOOLS or shell_budget_exhausted):
        result = {
            "success": False,
            "error": "measured progress required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "measured_progress_guard": measured_progress_guard,
            "guidance": (
                "This job is measurably framed and has exhausted its research budget without new experiment records. "
                "If the shell/action budget is exhausted, do not call shell_exec again; call record_experiment for a "
                "known measurement, record_tasks with an experiment/action/monitor contract, or record_lesson if "
                "measurement is blocked."
            ),
        }
        return result, f"blocked {name}; measured progress required"

    if name in BRANCH_WORK_TOOLS and _task_queue_exhausted(job):
        result = {
            "success": False,
            "error": "task branch required before more work",
            "blocked_tool": name,
            "blocked_arguments": args,
            "guidance": (
                "The durable task queue has no open or active branch. Use record_tasks to open the next concrete "
                "branch before doing more research or execution, or report_update if the operator needs a checkpoint."
            ),
        }
        return result, f"blocked {name}; no open task branch"

    known_bad_source = _known_bad_source_for_call(name, args, job)
    if known_bad_source:
        result = {
            "success": False,
            "error": "known bad source blocked",
            "blocked_tool": name,
            "blocked_arguments": args,
            "known_bad_source": known_bad_source,
            "guidance": (
                "The source ledger marks this source as blocked or low-yield for this job. "
                "Choose a different source, or record a fresh operator reason before retrying it."
            ),
        }
        return result, f"blocked {name}; known bad source {known_bad_source.get('source')}"

    if name == "web_search":
        similar_step = _similar_recent_search(args, recent_steps)
        if similar_step:
            result = {
                "success": False,
                "error": "similar search query blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "previous_step": similar_step["id"],
                "guidance": "Use an existing result URL, extract a page, or search a clearly different topic/location/source.",
            }
            return result, f"blocked similar web_search; previous step #{similar_step['step_no']}"
        streak = _recent_search_streak(recent_steps)
        if streak >= 3:
            result = {
                "success": False,
                "error": "search loop blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "recent_search_streak": streak,
                "guidance": "Stop searching. Extract or open one of the prior results, then write an artifact.",
            }
            return result, f"blocked web_search after {streak} consecutive searches"

    if name == "search_artifacts":
        similar_step = _similar_recent_query_tool("search_artifacts", args, recent_steps)
        if similar_step:
            result = {
                "success": False,
                "error": "similar artifact search blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "previous_step": similar_step["id"],
                "guidance": (
                    "Use a returned artifact, record what the prior artifact searches proved, "
                    "or create the next concrete task instead of searching saved outputs again."
                ),
            }
            return result, f"blocked similar search_artifacts; previous step #{similar_step['step_no']}"
        streak = _recent_tool_streak(recent_steps, "search_artifacts")
        if streak >= 3:
            result = {
                "success": False,
                "error": "artifact search loop blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "recent_artifact_search_streak": streak,
                "guidance": (
                    "Stop searching saved outputs. Read a specific returned artifact, update tasks/findings/lessons, "
                    "or write the next report artifact from already-read evidence."
                ),
            }
            return result, f"blocked search_artifacts after {streak} consecutive artifact searches"

    return None


def _summarize_tool_result(name: str, args: dict[str, Any], result: dict[str, Any], *, ok: bool) -> str:
    if not ok:
        return f"{name} failed: {result.get('error') or 'unknown error'}"
    if name == "web_search":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        top = "; ".join((item.get("title") or "untitled") for item in results[:3])
        return f"web_search query={args.get('query')!r} returned {len(results)} results: {top}"
    if name == "web_extract":
        pages = result.get("pages") if isinstance(result.get("pages"), list) else []
        ok_pages = [page for page in pages if not page.get("error")]
        return f"web_extract fetched {len(ok_pages)}/{len(pages)} pages"
    if name == "shell_exec":
        command = str(result.get("command") or args.get("command") or "")
        return (
            f"shell_exec rc={result.get('returncode')} "
            f"duration={result.get('duration_seconds')}s cmd={command!r}"
        )
    if name == "write_artifact":
        return f"write_artifact saved {result.get('artifact_id')} at {result.get('path')}"
    if name == "write_file":
        return f"write_file {result.get('mode') or 'overwrite'} {result.get('path')} bytes={result.get('bytes')}"
    if name == "defer_job":
        return f"defer_job until {result.get('defer_until')}"
    if name == "report_update":
        update = result.get("update") if isinstance(result.get("update"), dict) else {}
        return f"report_update saved: {str(update.get('message') or '')[:160]}"
    if name == "record_lesson":
        lesson = result.get("lesson") if isinstance(result.get("lesson"), dict) else {}
        category = lesson.get("category") or "memory"
        text = str(lesson.get("lesson") or "")[:160]
        return f"record_lesson saved {category}: {text}"
    if name == "record_source":
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        return f"record_source updated {source.get('source')} score={source.get('usefulness_score')} yield={source.get('yield_count')}"
    if name == "record_findings":
        return (
            f"record_findings updated ledger: {result.get('added', 0)} new, "
            f"{result.get('updated', 0)} updated, {result.get('sources_updated', 0)} sources"
        )
    if name == "record_tasks":
        return f"record_tasks updated queue: {result.get('added', 0)} new, {result.get('updated', 0)} updated"
    if name == "record_roadmap":
        roadmap = result.get("roadmap") if isinstance(result.get("roadmap"), dict) else {}
        return (
            f"record_roadmap {roadmap.get('status')}: {roadmap.get('title')} "
            f"milestones={len(roadmap.get('milestones') or [])}"
        )
    if name == "record_milestone_validation":
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        return (
            f"record_milestone_validation {validation.get('validation_status')}: "
            f"{validation.get('title')} followups={len(result.get('follow_up_tasks') or [])}"
        )
    if name == "record_experiment":
        experiment = result.get("experiment") if isinstance(result.get("experiment"), dict) else {}
        metric = ""
        if experiment.get("metric_value") is not None:
            metric = " " + format_metric_value(
                experiment.get("metric_name") or "metric",
                experiment.get("metric_value"),
                experiment.get("metric_unit") or "",
            )
        best = " best" if experiment.get("best_observed") else ""
        return f"record_experiment {experiment.get('status')}: {experiment.get('title')}{metric}{best}"
    if name == "acknowledge_operator_context":
        return f"acknowledge_operator_context {result.get('status')} count={result.get('count', 0)}"
    if name == "browser_navigate":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        title = data.get("title") or ""
        url = data.get("url") or ""
        warning = f" | warning={result.get('source_warning')}" if result.get("source_warning") else ""
        return f"browser_navigate opened {title} <{url}>{warning}"
    if name == "browser_snapshot":
        snapshot = str(result.get("snapshot") or result.get("data") or "")
        warning = f" | warning={result.get('source_warning')}" if result.get("source_warning") else ""
        return f"browser_snapshot returned {len(snapshot)} chars{warning}"
    if name == "read_artifact":
        return f"read_artifact read {result.get('artifact_id')}"
    if name == "search_artifacts":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        return f"search_artifacts returned {len(results)} results for {args.get('query')!r}"
    return f"{name} completed"


def _error_result(exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }
    if isinstance(exc, LLMResponseError) and exc.payload:
        result["provider_payload"] = exc.payload
    return result


def _hard_llm_provider_failure_note(exc: Exception) -> str:
    return provider_action_required_note(exc)


def _max_step_no(steps: list[dict[str, Any]]) -> int:
    return max((int(step.get("step_no") or 0) for step in steps), default=0)


def _should_reflect(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> bool:
    if not recent_steps:
        return False
    if recent_steps[-1].get("kind") == "reflection":
        return False
    step_no = _max_step_no(recent_steps)
    if step_no == 0 or step_no % REFLECTION_INTERVAL_STEPS != 0:
        return False
    reflections = _metadata_list(job, "reflections")
    if not reflections:
        return True
    last_reflected = 0
    metadata = reflections[-1].get("metadata") if isinstance(reflections[-1].get("metadata"), dict) else {}
    if isinstance(metadata.get("through_step"), int):
        last_reflected = metadata["through_step"]
    return step_no > last_reflected


def _claim_operator_queue(db: AgentDB, job_id: str) -> list[dict[str, Any]]:
    steering = db.claim_operator_messages(job_id, modes=("steer",), limit=1)
    if steering:
        return steering
    return db.claim_operator_messages(job_id, modes=("follow_up",), limit=1)


def _emit_loop_start(db: AgentDB, job_id: str, run_id: str) -> None:
    db.append_event(
        job_id,
        event_type="loop",
        title="agent_start",
        ref_table="job_runs",
        ref_id=run_id,
        metadata={"run_id": run_id},
    )
    db.append_event(
        job_id,
        event_type="loop",
        title="turn_start",
        ref_table="job_runs",
        ref_id=run_id,
        metadata={"run_id": run_id},
    )


def _emit_assistant_message_event(
    db: AgentDB,
    job_id: str,
    run_id: str,
    response: LLMResponse,
    *,
    messages: list[dict[str, Any]],
    context_length: int,
) -> None:
    if response.tool_calls:
        body = ", ".join(call.name for call in response.tool_calls)
        metadata = {"run_id": run_id, "tool_calls": [call.name for call in response.tool_calls]}
    else:
        body = response.content[:1000]
        metadata = {"run_id": run_id, "tool_calls": []}
    metadata["usage"] = turn_usage_metadata(response, messages=messages, context_length=context_length)
    if response.model:
        metadata["model"] = response.model
    if response.response_id:
        metadata["response_id"] = response.response_id
    db.append_event(
        job_id,
        event_type="loop",
        title="message_end",
        body=body,
        ref_table="job_runs",
        ref_id=run_id,
        metadata=metadata,
    )


def _emit_loop_end(
    db: AgentDB,
    job_id: str,
    run_id: str,
    *,
    status: str,
    step_id: str | None = None,
    tool_name: str | None = None,
    detail: str = "",
) -> None:
    metadata = {"run_id": run_id, "status": status, "step_id": step_id or "", "tool": tool_name or ""}
    db.append_event(
        job_id,
        event_type="loop",
        title="turn_end",
        body=detail[:1000],
        ref_table="job_runs",
        ref_id=run_id,
        metadata=metadata,
    )
    db.append_event(
        job_id,
        event_type="loop",
        title="agent_end",
        body=status,
        ref_table="job_runs",
        ref_id=run_id,
        metadata=metadata,
    )


def _run_reflection_step(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    db: AgentDB,
    job_id: str,
    run_id: str,
) -> StepExecution:
    step_id = db.add_step(job_id=job_id, run_id=run_id, kind="reflection", tool_name="reflect")
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    findings = metadata.get("finding_ledger") if isinstance(metadata.get("finding_ledger"), list) else []
    sources = metadata.get("source_ledger") if isinstance(metadata.get("source_ledger"), list) else []
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    experiments = metadata.get("experiment_ledger") if isinstance(metadata.get("experiment_ledger"), list) else []
    lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    validating_milestones = [
        milestone for milestone in milestones
        if isinstance(milestone, dict)
        and (
            str(milestone.get("status") or "planned") == "validating"
            or str(milestone.get("validation_status") or "not_started") == "pending"
        )
    ]
    operator_messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    active_operator_messages = [
        entry for entry in operator_messages
        if isinstance(entry, dict)
        and str(entry.get("mode") or "steer") in {"steer", "follow_up"}
        and not entry.get("acknowledged_at")
        and not entry.get("superseded_at")
    ]
    pending_measurement = _pending_measurement_obligation(job)
    artifacts = db.list_artifacts(job_id, limit=12)
    failures = [step for step in recent_steps[-REFLECTION_INTERVAL_STEPS:] if step.get("status") == "failed" or step.get("status") == "blocked"]
    step_no = _max_step_no(recent_steps)
    finding_batches = [artifact for artifact in artifacts if "finding" in str(artifact.get("title") or artifact.get("summary") or "").lower()]
    best_sources = sorted(
        [
            source for source in sources
            if isinstance(source, dict)
            and (
                _as_int(source.get("yield_count")) > 0
                or _as_float(source.get("usefulness_score")) >= 0.2
            )
            and _as_int(source.get("fail_count")) <= max(0, _as_int(source.get("yield_count")))
        ],
        key=lambda source: (_as_float(source.get("usefulness_score")), _as_int(source.get("yield_count"))),
        reverse=True,
    )[:3]
    source_text = ", ".join(str(source.get("source") or "") for source in best_sources) or "no high-yield source yet"
    measured_experiments = [experiment for experiment in experiments if isinstance(experiment, dict) and experiment.get("metric_value") is not None]
    best_experiments = [experiment for experiment in measured_experiments if experiment.get("best_observed")]
    best_experiment_text = "no measured experiment yet"
    if best_experiments:
        best_experiment_text = "; ".join(
            f"{experiment.get('title')} " + format_metric_value(
                experiment.get("metric_name") or "metric",
                experiment.get("metric_value"),
                experiment.get("metric_unit") or "",
            )
            for experiment in best_experiments[-3:]
        )
    summary = (
        f"Reflection through step #{step_no}: {len(findings)} findings, {len(sources)} sources, "
        f"{len(tasks)} tasks, {len(experiments)} experiments, {len(milestones)} roadmap milestones, "
        f"{len(lessons)} lessons, "
        f"{len(active_operator_messages)} active operator messages, "
        f"{len(finding_batches)} recent finding artifacts, {len(failures)} recent blocked/failed steps. "
        f"Best source direction: {source_text}. Best measured result: {best_experiment_text}."
        + (f" Roadmap '{roadmap.get('title')}' has {len(validating_milestones)} milestone(s) needing validation." if roadmap else "")
        + (" Pending measurement obligation needs resolution." if pending_measurement else "")
    )
    strategy = (
        "Prioritize source types that have yielded durable findings or artifacts; "
        "downgrade repetitive, blocked, or low-evidence paths that do not advance the objective. "
        "For measurable work, convert ideas into record_experiment trials and choose the next branch from the best observed result. "
        "For broad work, keep roadmap milestones compact and validate milestones from evidence before expanding scope."
    )
    reflection = db.append_reflection(
        job_id,
        summary,
        strategy=strategy,
        metadata={
            "through_step": step_no,
            "finding_count": len(findings),
            "source_count": len(sources),
            "task_count": len(tasks),
            "experiment_count": len(experiments),
            "roadmap_milestone_count": len(milestones),
            "roadmap_validation_needed_count": len(validating_milestones),
            "measured_experiment_count": len(measured_experiments),
            "active_operator_message_count": len(active_operator_messages),
            "pending_measurement_obligation": bool(pending_measurement),
        },
    )
    db.append_lesson(job_id, strategy, category="strategy", confidence=0.75, metadata={"source": "reflection", "through_step": step_no})
    db.append_agent_update(job_id, summary, category="plan", metadata={"reflection": reflection})
    result = {"success": True, "reflection": reflection}
    db.finish_step(step_id, status="completed", summary=summary, output_data=result)
    db.finish_run(run_id, "completed")
    _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, tool_name="reflect", detail=summary)
    refresh_memory_index(db, job_id)
    return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name="reflect", status="completed", result=result)


def _run_guard_recovery_step(
    context: dict[str, Any],
    *,
    db: AgentDB,
    job_id: str,
    run_id: str,
) -> StepExecution:
    error = str(context.get("error") or "recoverable guard")
    step_id = db.add_step(job_id=job_id, run_id=run_id, kind="recovery", tool_name="guard_recovery")
    lesson = db.append_lesson(
        job_id,
        (
            f"Repeated guard block '{error}' occurred {context.get('count')} times. "
            "Do not retry the same blocked tool pattern; update durable progress state, create a new branch, "
            "or explicitly reject the branch before continuing."
        ),
        category="strategy",
        confidence=0.75,
        metadata={"guard_recovery": context},
    )
    task = db.append_task_record(
        job_id,
        title=f"Resolve guard: {error}",
        status="open",
        priority=9,
        goal="Convert the repeated guard block into durable progress before retrying the blocked action.",
        output_contract="decision",
        acceptance_criteria=(
            "Use record_tasks, record_findings, record_source, record_experiment, or record_lesson to state what "
            "changed, what branch is rejected, or what concrete branch should run next."
        ),
        evidence_needed=f"Recent blocked tools: {', '.join(context.get('blocked_tools') or [])}",
        stall_behavior="If the same guard appears again, pivot to a different branch or record the branch as blocked.",
        metadata={"guard_recovery": context},
    )
    message = (
        f"Guard recovery opened a task after repeated '{error}' blocks "
        f"from step #{context.get('first_step_no')} to #{context.get('latest_step_no')}."
    )
    update = db.append_agent_update(
        job_id,
        message,
        category="blocked",
        metadata={"guard_recovery": context, "task_key": task.get("key"), "lesson_key": lesson.get("key")},
    )
    result = {
        "success": True,
        "guard_recovery": context,
        "lesson": lesson,
        "task": task,
        "update": update,
    }
    db.finish_step(step_id, status="completed", summary=message, output_data=result)
    db.finish_run(run_id, "completed")
    _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, tool_name="guard_recovery", detail=message)
    refresh_memory_index(db, job_id)
    return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name="guard_recovery", status="completed", result=result)


def _evidence_checkpoint_content(evidence_step: dict[str, Any]) -> str:
    output = evidence_step.get("output") if isinstance(evidence_step.get("output"), dict) else {}
    input_data = evidence_step.get("input") if isinstance(evidence_step.get("input"), dict) else {}
    observation = _observation_for_prompt(evidence_step.get("tool_name"), output)
    return "\n\n".join([
        "# Auto Evidence Checkpoint",
        f"Source step: #{evidence_step.get('step_no')} {evidence_step.get('tool_name') or evidence_step.get('kind')}",
        f"Summary: {evidence_step.get('summary') or ''}",
        f"Arguments:\n```json\n{json.dumps(input_data.get('arguments') or {}, ensure_ascii=False, indent=2)[:3000]}\n```",
        f"Observed:\n{observation or 'No compact observation available.'}",
        f"Raw output excerpt:\n```json\n{json.dumps(output, ensure_ascii=False, indent=2)[:9000]}\n```",
    ])


def _auto_persist_evidence(
    *,
    db: AgentDB,
    artifacts: ArtifactStore,
    job_id: str,
    run_id: str,
    step_id: str,
    blocked_tool: str,
    evidence_step: dict[str, Any],
) -> dict[str, Any]:
    stored = artifacts.write_text(
        job_id=job_id,
        run_id=run_id,
        step_id=step_id,
        title=f"Auto Evidence Checkpoint after step {evidence_step.get('step_no')}",
        summary=f"Auto-saved evidence before allowing more research; blocked tool was {blocked_tool}.",
        content=_evidence_checkpoint_content(evidence_step),
        artifact_type="text",
        metadata={"auto_checkpoint": True, "evidence_step": evidence_step.get("id"), "blocked_tool": blocked_tool},
    )
    lesson = db.append_lesson(
        job_id,
        (
            f"Evidence from step #{evidence_step.get('step_no')} must be persisted before more research; "
            f"auto-saved checkpoint {stored.id} after blocked {blocked_tool}."
        ),
        category="mistake",
        confidence=0.8,
        metadata={"artifact_id": stored.id, "blocked_tool": blocked_tool},
    )
    db.append_agent_update(
        job_id,
        f"Auto-saved evidence checkpoint {stored.id} after the model tried {blocked_tool} before persisting evidence.",
        category="blocked",
        metadata={"artifact_id": stored.id, "blocked_tool": blocked_tool},
    )
    return {"artifact_id": stored.id, "path": str(stored.path), "lesson": lesson}


def _auto_record_blocked_source(
    *,
    db: AgentDB,
    job_id: str,
    context: dict[str, Any],
    blocked_tool: str,
) -> dict[str, Any]:
    source = str(context.get("url") or context.get("title") or "unknown blocked browser source")
    reason = str(context.get("reason") or "anti-bot challenge")
    record = db.append_source_record(
        job_id,
        source,
        source_type="blocked_browser_source",
        usefulness_score=0.02,
        fail_count_delta=1,
        warnings=[reason],
        outcome=f"blocked by {reason}; pivot to an alternate source for the current objective",
        metadata={"blocked_tool": blocked_tool, "source_step": context.get("step_id")},
    )
    lesson = None
    if int(record.get("fail_count") or 0) <= 2:
        lesson = db.append_lesson(
            job_id,
            "Blocked, CAPTCHA, login, paywall, or anti-bot pages are not usable evidence for any long-running task; record the source outcome and pivot instead of repeating browser actions.",
            category="source_quality",
            confidence=0.9,
            metadata={"source": source, "blocked_tool": blocked_tool},
        )
    db.append_agent_update(
        job_id,
        f"Blocked source guard: current source is {reason}; pivoting away instead of looping.",
        category="blocked",
        metadata={"source": source, "blocked_tool": blocked_tool, "reason": reason},
    )
    return {"source": record, "lesson": lesson}


def _auto_record_tool_source_quality(
    *,
    db: AgentDB,
    job_id: str,
    tool_name: str | None,
    result: dict[str, Any],
) -> None:
    if tool_name == "web_extract":
        pages = result.get("pages") if isinstance(result.get("pages"), list) else []
        for page in pages[:12]:
            if not isinstance(page, dict):
                continue
            url = str(page.get("url") or "").strip()
            if not url:
                continue
            text = str(page.get("text") or "")
            error = str(page.get("error") or "")
            if error:
                db.append_source_record(
                    job_id,
                    url,
                    source_type="web_extract",
                    usefulness_score=0.1,
                    fail_count_delta=1,
                    warnings=[error[:180]],
                    outcome=f"extract failed: {error[:180]}",
                    metadata={"auto_from_tool": "web_extract"},
                )
                continue
            score = 0.35
            if len(text.strip()) >= 500:
                score = 0.55
            if len(text.strip()) >= 3000:
                score = 0.7
            db.append_source_record(
                job_id,
                url,
                source_type="web_extract",
                usefulness_score=score,
                yield_count=0,
                outcome=f"extracted {len(text.strip())} chars for possible use",
                metadata={"auto_from_tool": "web_extract"},
            )
        return
    if tool_name in {"browser_navigate", "browser_snapshot"}:
        context = _browser_warning_context(result)
        if not context:
            return
        result["source_warning"] = context["reason"]
        result["source_url"] = context.get("url") or ""
        _auto_record_blocked_source(db=db, job_id=job_id, context=context, blocked_tool=tool_name or "browser")


def _auto_reconcile_artifact_tasks(
    *,
    db: AgentDB,
    job_id: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    artifact_id = str(result.get("artifact_id") or "")
    if not artifact_id:
        return []
    artifact_title = str(args.get("title") or "")
    artifact_summary = str(args.get("summary") or "")
    artifact_content = str(args.get("content") or "")
    artifact_text = " ".join([artifact_title, artifact_summary, artifact_content[:4000]])
    artifact_tokens = _text_tokens(artifact_text)
    if len(artifact_tokens) < 2:
        return []
    job = db.get_job(job_id)
    reconciled = []
    for task in _metadata_list(job, "task_queue"):
        status = str(task.get("status") or "open").strip().lower()
        if status not in {"open", "active"}:
            continue
        contract = str(task.get("output_contract") or "").strip().lower()
        if contract in {"experiment", "action", "monitor"}:
            continue
        task_text = " ".join(
            str(task.get(key) or "")
            for key in ("title", "goal", "acceptance_criteria", "evidence_needed", "source_hint")
        )
        if not _artifact_can_reconcile_task(
            contract=contract,
            task_text=task_text,
            artifact_title=artifact_title,
            artifact_summary=artifact_summary,
        ):
            continue
        task_tokens = _text_tokens(task_text)
        if len(task_tokens) < 2:
            continue
        overlap = task_tokens & artifact_tokens
        needed = max(2, min(4, (len(task_tokens) + 1) // 2))
        if len(overlap) < needed:
            continue
        updated = db.append_task_record(
            job_id,
            title=str(task.get("title") or ""),
            status="done",
            priority=_as_int(task.get("priority")),
            goal=str(task.get("goal") or ""),
            source_hint=str(task.get("source_hint") or ""),
            result=f"Saved output {artifact_id}: {_clip_text(artifact_title or artifact_summary, 180)}",
            parent=str(task.get("parent") or ""),
            output_contract=contract,
            acceptance_criteria=str(task.get("acceptance_criteria") or ""),
            evidence_needed=str(task.get("evidence_needed") or ""),
            stall_behavior=str(task.get("stall_behavior") or ""),
            metadata={
                **(task.get("metadata") if isinstance(task.get("metadata"), dict) else {}),
                "auto_reconciled_from_artifact": artifact_id,
                "matched_tokens": sorted(overlap)[:12],
            },
        )
        reconciled.append(updated)
    if reconciled:
        titles = ", ".join(str(task.get("title") or "") for task in reconciled[:4])
        db.append_agent_update(
            job_id,
            f"Task progress reconciled from saved output {artifact_id}: {titles}.",
            category="plan",
            metadata={"artifact_id": artifact_id, "task_count": len(reconciled)},
        )
    return reconciled


def _artifact_can_reconcile_task(
    *,
    contract: str,
    task_text: str,
    artifact_title: str,
    artifact_summary: str,
) -> bool:
    contract = contract.strip().lower()
    if contract in {"experiment", "action", "monitor"}:
        return False
    if contract == "research":
        return True
    artifact_text = f"{artifact_title} {artifact_summary}".lower()
    task_lower = task_text.lower()
    evidence_like = any(term in artifact_text for term in EVIDENCE_ARTIFACT_TERMS)
    deliverable_like = any(term in artifact_text for term in DELIVERABLE_ARTIFACT_TERMS)
    task_needs_deliverable_action = any(term in task_lower for term in TASK_DELIVERABLE_ACTION_TERMS)
    if evidence_like:
        return False
    if task_needs_deliverable_action and not deliverable_like:
        return False
    return True


def _auto_checkpoint_update(
    *,
    db: AgentDB,
    job_id: str,
    step_no: int,
    tool_name: str | None,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    title_text = " ".join(str(args.get(key) or "") for key in ("title", "summary", "type")).lower()
    is_finding_batch = tool_name == "write_artifact" and "finding" in title_text
    if not is_finding_batch and step_no % 10 != 0:
        return
    job = db.get_job(job_id)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    previous = metadata.get("last_checkpoint_counts") if isinstance(metadata.get("last_checkpoint_counts"), dict) else {}
    checkpoint = build_progress_checkpoint(
        metadata,
        previous_counts=previous,
        step_no=step_no,
        tool_name=tool_name,
        artifact_id=str(result.get("artifact_id") or ""),
        is_finding_output=is_finding_batch,
    )
    db.append_agent_update(
        job_id,
        checkpoint.message,
        category=checkpoint.category,
        metadata={"step_no": step_no, "tool": tool_name, "deltas": checkpoint.deltas},
    )
    streak = _as_int(metadata.get("activity_checkpoint_streak"))
    streak = streak + 1 if checkpoint.category == "activity" else 0
    db.update_job_metadata(
        job_id,
        {
            "last_checkpoint_counts": checkpoint.counts,
            "activity_checkpoint_streak": streak,
        },
    )


def _execute_tool_call(
    call: Any,
    *,
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    config: AppConfig,
    db: AgentDB,
    artifacts: ArtifactStore,
    registry: ToolRegistry,
    job_id: str,
    run_id: str,
) -> tuple[StepExecution, bool, str, str | None]:
    step_id = db.add_step(
        job_id=job_id,
        run_id=run_id,
        kind="tool",
        tool_name=call.name,
        input_data={"tool_call_id": call.id, "arguments": call.arguments},
    )
    blocked = _blocked_tool_call_result(call.name, call.arguments, recent_steps, job)
    if blocked:
        result, summary = blocked
        result = {**result, "success": True, "recoverable": True}
        evidence_checkpoint = None
        if result.get("error") == "artifact required before more research":
            evidence_step = next(
                (step for step in recent_steps if step.get("id") == result.get("previous_step")),
                None,
            )
            if evidence_step:
                evidence_checkpoint = _auto_persist_evidence(
                    db=db,
                    artifacts=artifacts,
                    job_id=job_id,
                    run_id=run_id,
                    step_id=step_id,
                    blocked_tool=call.name,
                    evidence_step=evidence_step,
                )
                result["auto_checkpoint"] = evidence_checkpoint
                summary = f"blocked {call.name}; auto-saved evidence checkpoint {evidence_checkpoint['artifact_id']}"
        anti_bot_source = result.get("anti_bot_source") if isinstance(result.get("anti_bot_source"), dict) else None
        if anti_bot_source:
            result["auto_source_record"] = _auto_record_blocked_source(
                db=db,
                job_id=job_id,
                context=anti_bot_source,
                blocked_tool=call.name,
            )
        known_bad_source = result.get("known_bad_source") if isinstance(result.get("known_bad_source"), dict) else None
        if known_bad_source:
            db.append_agent_update(
                job_id,
                f"Source ledger blocked retry of {known_bad_source.get('source')}; choosing a different route next.",
                category="blocked",
                metadata={"source": known_bad_source, "blocked_tool": call.name},
            )
        db.finish_step(
            step_id,
            status="blocked",
            summary=summary,
            output_data=result,
            error=None,
        )
        return (
            StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=call.name, status="blocked", result=result),
            True,
            summary,
            None,
        )

    ctx = ToolContext(
        config=config,
        db=db,
        artifacts=artifacts,
        job_id=job_id,
        run_id=run_id,
        step_id=step_id,
        task_id=job_id,
    )
    try:
        raw_result = registry.handle(call.name, call.arguments, ctx)
        result = _parse_tool_result(raw_result)
        ok = bool(result.get("success", True)) and not result.get("error")
        status = "completed" if ok else "failed"
        if ok:
            _auto_record_tool_source_quality(db=db, job_id=job_id, tool_name=call.name, result=result)
        summary = _summarize_tool_result(call.name, call.arguments, result, ok=ok)
        db.finish_step(step_id, status=status, summary=summary, output_data=result, error=result.get("error"))
        if ok:
            finished_step = _step_by_id(db, job_id, step_id)
            _maybe_create_measurement_obligation(
                db=db,
                job_id=job_id,
                step=finished_step,
                tool_name=call.name,
                args=call.arguments,
                result=result,
            )
            _auto_checkpoint_update(
                db=db,
                job_id=job_id,
                step_no=(finished_step or db.list_steps(job_id=job_id)[-1])["step_no"],
                tool_name=call.name,
                args=call.arguments,
                result=result,
            )
            if call.name == "write_artifact":
                reconciled_tasks = _auto_reconcile_artifact_tasks(
                    db=db,
                    job_id=job_id,
                    args=call.arguments,
                    result=result,
                )
                if reconciled_tasks:
                    result["auto_reconciled_tasks"] = [
                        {"title": task.get("title"), "status": task.get("status")}
                        for task in reconciled_tasks[:8]
                    ]
        return (
            StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=call.name, status=status, result=result),
            status != "completed",
            summary,
            result.get("error") if status == "failed" else None,
        )
    except Exception as exc:
        result = _error_result(exc)
        db.finish_step(step_id, status="failed", summary=f"{call.name} raised", output_data=result, error=str(exc))
        return (
            StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=call.name, status="failed", result=result),
            True,
            str(exc),
            str(exc),
        )


def run_one_step(
    job_id: str,
    *,
    config: AppConfig | None = None,
    db: AgentDB | None = None,
    llm: StepLLM | None = None,
    registry: ToolRegistry = DEFAULT_REGISTRY,
) -> StepExecution:
    config = config or load_config()
    config.ensure_dirs()
    owns_db = db is None
    db = db or AgentDB(config.runtime.state_db_path)
    try:
        artifacts = ArtifactStore(config.runtime.home, db=db)
        job = db.get_job(job_id)
        if _acknowledge_non_prompt_operator_context(db, job_id):
            job = db.get_job(job_id)
        if _clear_invalid_measurement_obligation(db, job_id):
            job = db.get_job(job_id)
        run_id = db.start_run(job_id, model=config.model.model)
        _emit_loop_start(db, job_id, run_id)
        recent_steps = db.list_steps(job_id=job_id)
        if _should_reflect(job, recent_steps):
            return _run_reflection_step(job, recent_steps, db=db, job_id=job_id, run_id=run_id)
        guard_recovery = _repeated_guard_block_context(recent_steps)
        if guard_recovery:
            return _run_guard_recovery_step(guard_recovery, db=db, job_id=job_id, run_id=run_id)
        active_operator_messages = _claim_operator_queue(db, job_id)
        if active_operator_messages:
            job = db.get_job(job_id)
        messages = build_messages(
            job,
            recent_steps,
            memory_entries=db.list_memory(job_id),
            program_text=_load_program_text(config, job_id),
            timeline_events=db.list_timeline_events(job_id, limit=30),
            active_operator_messages=active_operator_messages,
            include_unclaimed_operator_messages=False,
        )
        llm = llm or OpenAIChatLLM(config.model)
        try:
            response: LLMResponse = llm.next_action(messages=messages, tools=registry.openai_tools())
        except Exception as exc:
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="llm",
                status="failed",
                summary=f"model call failed: {type(exc).__name__}",
                input_data={"model": config.model.model},
            )
            result = _error_result(exc)
            hard_failure_note = _hard_llm_provider_failure_note(exc)
            if hard_failure_note:
                result["provider_action_required"] = True
                result["pause_reason"] = "llm_provider_blocked"
                db.update_job_status(
                    job_id,
                    "paused",
                    metadata_patch={
                        "last_note": hard_failure_note,
                        "provider_blocked_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                db.append_agent_update(
                    job_id,
                    hard_failure_note,
                    category="error",
                    metadata={"reason": "llm_provider_blocked", "error_type": type(exc).__name__},
                )
            db.finish_step(step_id, status="failed", output_data=result, error=str(exc))
            db.finish_run(run_id, "failed", error=str(exc))
            _emit_loop_end(db, job_id, run_id, status="failed", step_id=step_id, detail=str(exc))
            refresh_memory_index(db, job_id)
            return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=None, status="failed", result=result)

        _emit_assistant_message_event(
            db,
            job_id,
            run_id,
            response,
            messages=messages,
            context_length=config.model.context_length,
        )

        if response.tool_calls:
            executions: list[StepExecution] = []
            details: list[str] = []
            run_error: str | None = None
            for call in response.tool_calls:
                current_job = db.get_job(job_id)
                current_recent_steps = db.list_steps(job_id=job_id)
                execution, stop_batch, detail, error = _execute_tool_call(
                    call,
                    job=current_job,
                    recent_steps=current_recent_steps,
                    config=config,
                    db=db,
                    artifacts=artifacts,
                    registry=registry,
                    job_id=job_id,
                    run_id=run_id,
                )
                executions.append(execution)
                details.append(detail)
                if error:
                    run_error = error
                if stop_batch:
                    break

            final_execution = executions[-1]
            run_status = "failed" if any(item.status == "failed" for item in executions) else "completed"
            db.finish_run(run_id, run_status, error=run_error)
            detail = f"executed {len(executions)}/{len(response.tool_calls)} tool calls"
            if details:
                detail = f"{detail}; last: {details[-1]}"
            _emit_loop_end(
                db,
                job_id,
                run_id,
                status=final_execution.status,
                step_id=final_execution.step_id,
                tool_name=final_execution.tool_name,
                detail=detail,
            )
            refresh_memory_index(db, job_id)
            return final_execution

        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="assistant",
            status="completed",
            summary=response.content[:500],
            input_data={},
        )
        result = {"success": True, "content": response.content}
        db.finish_step(step_id, status="completed", output_data=result)
        db.finish_run(run_id, "completed")
        _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, detail=response.content[:1000])
        refresh_memory_index(db, job_id)
        return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=None, status="completed", result=result)
    finally:
        if owns_db:
            db.close()
