"""Durable outcome summaries for the Nipux terminal UI."""

from __future__ import annotations

import textwrap
from typing import Any

from nipux_cli.tui_event_format import (
    brief_reflection_text,
    chat_agent_message_text,
    event_clock,
    event_hour,
    event_title_body,
    event_tool_args,
    experiment_metric_text,
    generic_display_text,
    shell_write_target,
    short_path,
    tool_live_summary,
)
from nipux_cli.tui_style import _bold, _event_badge, _fit_ansi, _muted, _one_line, _page_indicator, _strip_ansi


CHAT_RIGHT_PAGES = [("status", "Status"), ("updates", "Outcomes"), ("work", "Work")]

DURABLE_OUTCOME_LABELS = {
    "SAVE",
    "FIND",
    "SOURCE",
    "TEST",
    "TASK",
    "ROAD",
    "VALID",
    "LEARN",
    "FILE",
}

SUMMARY_COUNT_LABELS = DURABLE_OUTCOME_LABELS | {"DONE", "FAIL"}
PRIMARY_OUTCOME_LABELS = DURABLE_OUTCOME_LABELS | {"FAIL"}

OUTCOME_SUMMARY_NAMES = {
    "SAVE": "outputs",
    "FIND": "findings",
    "SOURCE": "sources",
    "TEST": "measurements",
    "TASK": "tasks",
    "ROAD": "roadmap",
    "VALID": "validations",
    "LEARN": "lessons",
    "PLAN": "plans",
    "UPDATE": "updates",
    "FAIL": "blocks",
    "FILE": "files",
    "DONE": "research",
}

SUMMARY_EVENT_TYPES = (
    "agent_message",
    "artifact",
    "error",
    "experiment",
    "finding",
    "lesson",
    "milestone_validation",
    "reflection",
    "roadmap",
    "source",
    "task",
)

SUMMARY_TOOL_EVENT_TYPES = ("tool_result",)


def model_update_event_parts(event: dict[str, Any], *, width: int, compact: bool = True) -> tuple[str, str, str] | None:
    kind = str(event.get("event_type") or "")
    title = generic_display_text(event.get("title") or "")
    body = generic_display_text(event.get("body") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    status = str(metadata.get("status") or "")
    clock = event_clock(event)
    chars = max(24, width - 16)
    if kind == "artifact":
        detail = event_title_body(title, body or str(metadata.get("summary") or ""), fallback="saved output")
        return "SAVE", _outcome_text(detail, chars=chars, compact=compact), clock
    if kind == "finding":
        return "FIND", _outcome_text(event_title_body(title, body, fallback="finding"), chars=chars, compact=compact), clock
    if kind == "source":
        return "SOURCE", _outcome_text(event_title_body(title, body, fallback="source"), chars=chars, compact=compact), clock
    if kind == "experiment":
        metric = experiment_metric_text(metadata)
        detail = event_title_body(title, body, fallback="measurement")
        if metric and metric not in detail:
            detail = f"{detail} - {metric}"
        return "TEST", _outcome_text(detail, chars=chars, compact=compact), clock
    if kind == "task":
        task_status = str(metadata.get("status") or "")
        detail = event_title_body(title, body, fallback="task")
        prefix = f"{task_status} " if task_status else ""
        return "TASK", _outcome_text(prefix + detail, chars=chars, compact=compact), clock
    if kind == "roadmap":
        return "ROAD", _outcome_text(event_title_body(title, body, fallback="roadmap"), chars=chars, compact=compact), clock
    if kind == "milestone_validation":
        validation = str(metadata.get("validation_status") or metadata.get("status") or "")
        detail = event_title_body(title, body, fallback="milestone")
        return "VALID", _outcome_text(f"{validation} {detail}".strip(), chars=chars, compact=compact), clock
    if kind == "lesson":
        return "LEARN", _outcome_text(event_title_body(title, body, fallback="lesson"), chars=chars, compact=compact), clock
    if kind == "reflection":
        return "PLAN", _outcome_text(brief_reflection_text(body or title), chars=chars, compact=compact), clock
    if kind == "agent_message" and title.lower() in {"error", "blocked"}:
        detail = chat_agent_message_text(title, body) or event_title_body(title, body, fallback="error")
        return "FAIL", _outcome_text(detail, chars=chars, compact=compact), clock
    if kind == "agent_message" and title.lower() in {"progress", "update", "report", "plan", "planning"}:
        detail = chat_agent_message_text(title, body) or event_title_body(title, body, fallback="update")
        return "UPDATE", _outcome_text(detail, chars=chars, compact=compact), clock
    if kind == "tool_result" and status == "completed":
        tool = title
        if tool in {"web_search", "web_extract"}:
            return "DONE", _outcome_text(tool_live_summary(tool, metadata, body), chars=chars, compact=compact), clock
        if tool == "shell_exec":
            command = str(event_tool_args(metadata).get("command") or "")
            target = shell_write_target(command)
            if target:
                return "FILE", _outcome_text(f"updated {short_path(target, max_width=chars - 8)} via shell", chars=chars, compact=compact), clock
        if tool == "write_file":
            output = metadata.get("output") if isinstance(metadata.get("output"), dict) else {}
            path = str(output.get("path") or event_tool_args(metadata).get("path") or "")
            return "FILE", _outcome_text(f"updated {short_path(path, max_width=chars - 8)}", chars=chars, compact=compact), clock
    return None


def is_summary_event_candidate(event: dict[str, Any]) -> bool:
    kind = str(event.get("event_type") or "")
    if kind in SUMMARY_EVENT_TYPES:
        return True
    if kind != "tool_result":
        return False
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if str(metadata.get("status") or "") != "completed":
        return False
    title = str(event.get("title") or "")
    if title == "write_file":
        return True
    if title == "shell_exec":
        command = str(event_tool_args(metadata).get("command") or "")
        return bool(shell_write_target(command))
    return False


def latest_durable_outcome_line(events: list[dict[str, Any]], *, width: int) -> str:
    fallback: tuple[str, str, str] | None = None
    for event in reversed(events):
        parsed = model_update_event_parts(event, width=width)
        if not parsed:
            continue
        label, text, _clock = parsed
        if label == "DONE":
            fallback = fallback or parsed
            continue
        if label not in PRIMARY_OUTCOME_LABELS:
            continue
        prefix = f"{_muted('Outcome')} {_event_badge(label)} "
        return _fit_ansi(prefix + _one_line(text, max(12, width - len(_strip_ansi(prefix)))), width)
    if fallback:
        label, text, _clock = fallback
        prefix = f"{_muted('Outcome')} {_event_badge(label)} "
        return _fit_ansi(prefix + _one_line(text, max(12, width - len(_strip_ansi(prefix)))), width)
    return ""


def recent_model_update_lines(
    events: list[dict[str, Any]],
    *,
    width: int,
    limit: int,
    include_research: bool = False,
) -> list[str]:
    """Render recent durable worker outcomes for the compact status pane."""
    if limit <= 0:
        return []
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for event in reversed(events):
        parsed = model_update_event_parts(event, width=max(width, 180))
        if not parsed:
            continue
        label, text, clock = parsed
        if label == "DONE" and not include_research:
            continue
        if label not in PRIMARY_OUTCOME_LABELS and not (include_research and label == "DONE"):
            continue
        key = (label, text)
        if key in seen:
            continue
        seen.add(key)
        prefix = f"{_muted(clock)} {_event_badge(label)} " if clock else f"{_event_badge(label)} "
        available = max(12, width - len(_strip_ansi(prefix)))
        wrapped = textwrap.wrap(text, width=available) or [""]
        lines.append(_fit_ansi(prefix + wrapped[0], width))
        if len(lines) >= limit:
            return lines
        continuation_prefix = " " * len(_strip_ansi(prefix))
        for part in wrapped[1:2]:
            lines.append(_fit_ansi(continuation_prefix + part, width))
            if len(lines) >= limit:
                return lines
        if len(lines) >= limit:
            return lines
    return lines


def chat_updates_pane_lines(
    *,
    job: dict[str, Any],
    events: list[dict[str, Any]],
    width: int,
    rows: int,
) -> list[str]:
    lines = [
        f"{_muted('Page')}   {_page_indicator('updates', CHAT_RIGHT_PAGES)}",
        f"{_muted('Focus')}  {_bold(_one_line(job.get('title') or 'untitled', width - 8))}",
        "",
        _bold("Outcomes by hour"),
        _muted("Summaries of durable output, findings, measurements, decisions, and files."),
        "",
    ]
    update_lines = hourly_update_lines(events, width=width, limit=max(4, rows - len(lines)))
    if update_lines:
        lines.extend(update_lines)
    else:
        lines.append(_muted("No durable model updates yet. Tool calls are on Work."))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def hourly_update_lines(events: list[dict[str, Any]], *, width: int, limit: int) -> list[str]:
    if limit <= 0:
        return []
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        parsed = model_update_event_parts(event, width=max(width, 220), compact=False)
        if not parsed:
            continue
        label, text, clock = parsed
        if label not in SUMMARY_COUNT_LABELS:
            continue
        hour = event_hour(event)
        if hour not in buckets:
            buckets[hour] = {"counts": {}, "items": [], "clock": clock}
            order.append(hour)
        bucket = buckets[hour]
        counts = bucket["counts"]
        counts[label] = int(counts.get(label) or 0) + 1
        item = (label, text)
        if item not in bucket["items"]:
            bucket["items"].append(item)
    rendered: list[str] = []
    # Each visible hour needs a header and at least a couple durable outcomes.
    # Showing too many buckets makes the pane churn and can trim off the hour
    # label, which is harder to scan during long-running jobs.
    max_visible_hours = max(1, min(len(order), max(1, limit // 4)))
    recent_hours = order[-max_visible_hours:]
    available_items = max(1, limit - len(recent_hours))
    per_bucket = max(1, min(6, available_items // max(1, len(recent_hours))))
    for hour in recent_hours:
        bucket = buckets[hour]
        counts = bucket["counts"]
        summary = hourly_outcome_summary(counts)
        rendered.append(_fit_ansi(f"{_muted(hour)} {_bold(summary or 'activity')}", width))
        primary_items = [item for item in bucket["items"] if item[0] in PRIMARY_OUTCOME_LABELS]
        visible_items = primary_items or bucket["items"]
        for label, text in visible_items[-per_bucket:]:
            prefix = f"  {_event_badge(label)} "
            available = max(16, width - len(_strip_ansi(prefix)))
            parts = textwrap.wrap(text, width=available) or [""]
            rendered.append(_fit_ansi(prefix + parts[0], width))
            for part in parts[1:]:
                rendered.append(_fit_ansi(" " * len(_strip_ansi(prefix)) + part, width))
                if len(rendered) >= limit:
                    return rendered[:limit]
        if len(rendered) >= limit:
            return rendered[:limit]
    return rendered[:limit]


def hourly_outcome_summary(counts: dict[str, Any]) -> str:
    pieces: list[str] = []
    for label in sorted(counts):
        count = int(counts.get(label) or 0)
        if count <= 0:
            continue
        name = OUTCOME_SUMMARY_NAMES.get(label, label.lower())
        pieces.append(f"{count} {name}")
    return " ".join(pieces)


def _outcome_text(text: str, *, chars: int, compact: bool) -> str:
    clean = generic_display_text(text)
    if compact:
        return _one_line(clean, chars)
    return _one_line(clean, 900)
