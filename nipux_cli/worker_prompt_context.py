"""Prompt-context renderers for the Nipux worker loop."""

from __future__ import annotations

from typing import Any

from nipux_cli.metric_format import format_metric_value
from nipux_cli.operator_context import active_prompt_operator_entries, operator_entry_is_prompt_relevant
from nipux_cli.worker_policy import (
    MAX_WORKER_PROMPT_CHARS,
    PROMPT_SECTION_BUDGETS,
    SECTION_ITEM_CHARS,
    TIMELINE_PROMPT_AGENT_TITLES,
    TIMELINE_PROMPT_EVENT_TYPES,
    TIMELINE_PROMPT_EVENTS,
    TIMELINE_PROMPT_TOOL_STATUSES,
)
from nipux_cli.worker_prompt_format import clip_text as _clip_text


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
        and _operator_message_visible_in_prompt(entry, include_unclaimed=include_unclaimed)
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


def _operator_message_visible_in_prompt(entry: dict[str, Any], *, include_unclaimed: bool) -> bool:
    mode = str(entry.get("mode") or "steer").strip().lower().replace("-", "_")
    if entry.get("claimed_at") or mode == "note":
        return True
    return include_unclaimed and mode == "steer"


def _metadata_list(job: dict[str, Any], key: str) -> list[dict[str, Any]]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    values = metadata.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


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
