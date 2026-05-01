"""Status and work-pane renderers for the Nipux terminal UI."""

from __future__ import annotations

import textwrap
from typing import Any

from nipux_cli.operator_context import active_prompt_operator_entries
from nipux_cli.scheduling import job_deferred_until, job_provider_blocked
from nipux_cli.tui_event_format import experiment_metric_text
from nipux_cli.tui_events import (
    worker_activity_lines,
)
from nipux_cli.tui_outcomes import (
    CHAT_RIGHT_PAGES,
    latest_durable_outcome_line,
    model_update_event_parts,
    recent_model_update_lines,
)
from nipux_cli.tui_layout import _format_compact_count, _metric_strip
from nipux_cli.tui_style import (
    _accent,
    _bold,
    _event_badge,
    _fit_ansi,
    _muted,
    _one_line,
    _page_indicator,
    _status_badge,
    _strip_ansi,
)


def worker_label(job: dict[str, Any], daemon_running: bool) -> str:
    status = str(job.get("status") or "")
    if job_provider_blocked(job):
        return "blocked"
    if status == "planning":
        return "waiting"
    if status in {"paused", "completed", "cancelled", "failed"}:
        return status
    if job_deferred_until(job):
        return "waiting"
    return "active" if daemon_running and status in {"running", "queued"} else "idle"


def job_display_state(job: dict[str, Any], daemon_running: bool) -> str:
    status = str(job.get("status") or "")
    if job_provider_blocked(job):
        return "blocked"
    if status in {"running", "queued"}:
        if job_deferred_until(job):
            return "waiting"
        return "advancing" if daemon_running else "open"
    return status or "unknown"


def active_operator_messages(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    return [
        entry
        for entry in messages
        if isinstance(entry, dict)
        and entry in active_prompt_operator_entries(messages)
        and str(entry.get("mode") or "steer") in {"steer", "follow_up"}
    ]


def right_pane_lines(
    *,
    job: dict[str, Any],
    jobs: list[dict[str, Any]],
    job_artifacts: dict[str, list[dict[str, Any]]],
    job_summary_events: dict[str, list[dict[str, Any]]],
    job_counts: dict[str, dict[str, Any]],
    job_id: str,
    daemon_running: bool,
    state: str,
    worker: str,
    daemon_text: str,
    model: str,
    goal_text: str,
    latest_text: str,
    metrics: list[tuple[str, Any]],
    events: list[dict[str, Any]],
    token_usage: dict[str, Any],
    context_length: int,
    width: int,
    rows: int,
    right_view: str = "status",
) -> list[str]:
    del model
    info_lines = _chat_workspace_lines(
        right_view=right_view,
        job=job,
        state=state,
        worker=worker,
        daemon_text=daemon_text,
        goal_text=goal_text,
        latest_text=latest_text,
        metrics=metrics,
        token_usage=token_usage,
        context_length=context_length,
        width=width,
    )
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    active_operator = active_operator_messages(metadata)
    pending_measurement = (
        metadata.get("pending_measurement_obligation")
        if isinstance(metadata.get("pending_measurement_obligation"), dict)
        else {}
    )
    if active_operator:
        info_lines.append(f"{_muted('Operator')} {len(active_operator)} active")
        info_lines.append(f"{_muted('Context')} {_one_line(active_operator[-1].get('message') or '', width - 8)}")
    if pending_measurement:
        info_lines.append(f"{_muted('Measure')} pending step #{pending_measurement.get('source_step_no') or '?'}")
    if job_provider_blocked(job):
        info_lines.append(_fit_ansi(f"{_muted('Provider')} action needed before retrying model calls", width))
    defer_line = _defer_status_line(job, width=width)
    if defer_line:
        info_lines.append(defer_line)
    latest_outcome = latest_durable_outcome_line(events, width=width)
    if latest_outcome:
        info_lines.append(latest_outcome)
    info_lines.append("")
    info_lines.append(_bold("Jobs"))
    info_lines.extend(
        frame_jobs_lines(
            jobs[:5],
            focused_job_id=job_id,
            daemon_running=daemon_running,
            width=width,
            job_artifacts=job_artifacts,
            job_summary_events=job_summary_events,
            job_counts=job_counts,
            show_outputs=True,
        )
    )
    info_lines.append("")
    info_lines.append(_bold("Recent outcomes"))
    outcome_lines = recent_model_update_lines(events, width=width, limit=max(3, rows - len(info_lines)))
    if outcome_lines:
        info_lines.extend(outcome_lines)
    else:
        current_outputs = job_artifacts.get(job_id) or []
        if current_outputs:
            for artifact in current_outputs[:4]:
                title = _one_line(str(artifact.get("title") or artifact.get("id") or "output"), max(10, width - 8))
                info_lines.append(_fit_ansi(f"{_event_badge('SAVE')} {title}", width))
        else:
            info_lines.append(_muted("No durable outcomes yet."))
    return info_lines[:rows]


def chat_work_pane_lines(
    *,
    job: dict[str, Any],
    events: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    width: int,
    rows: int,
) -> list[str]:
    lines = [
        f"{_muted('Page')}   {_page_indicator('work', CHAT_RIGHT_PAGES)}",
        f"{_muted('Focus')}  {_bold(_one_line(job.get('title') or 'untitled', width - 8))}",
        "",
        _bold("Tool / console"),
    ]
    tool_lines = worker_activity_lines(events, width=width, limit=max(4, rows // 2))
    if tool_lines:
        lines.extend(tool_lines)
    else:
        lines.append(_muted("No recent tool calls."))
    remaining = max(0, rows - len(lines))
    if remaining > 4:
        lines.append("")
        lines.append(_bold("Tasks"))
        for task in _rank_visible_tasks(tasks)[: max(1, remaining // 2)]:
            status = str(task.get("status") or "open")
            title = _one_line(str(task.get("title") or "task"), max(10, width - 15))
            lines.append(_fit_ansi(f"{_status_badge(status)} {title}", width))
    remaining = max(0, rows - len(lines))
    if remaining > 3 and experiments:
        lines.append("")
        lines.append(_bold("Measurements"))
        for experiment in experiments[-max(1, remaining - 2) :]:
            metric = experiment_metric_text(experiment)
            title = _one_line(str(experiment.get("title") or "experiment"), max(10, width - 16))
            suffix = f" {_muted(metric)}" if metric else ""
            lines.append(_fit_ansi(f"{_event_badge('TEST')} {title}{suffix}", width))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def frame_jobs_lines(
    jobs: list[dict[str, Any]],
    *,
    focused_job_id: str,
    daemon_running: bool,
    width: int,
    job_artifacts: dict[str, list[dict[str, Any]]] | None = None,
    job_summary_events: dict[str, list[dict[str, Any]]] | None = None,
    job_counts: dict[str, dict[str, Any]] | None = None,
    show_outputs: bool = False,
) -> list[str]:
    rendered = []
    for index, item in enumerate(jobs[:5], start=1):
        item_id = str(item.get("id") or "")
        marker = _accent("●") if item_id == focused_job_id else _muted("○")
        title_width = max(14, min(30, width - 34))
        title = _one_line(str(item.get("title") or item.get("id") or "job"), title_width)
        state = _status_badge(job_display_state(item, daemon_running))
        worker = _status_badge(worker_label(item, daemon_running))
        kind = _one_line(item.get("kind") or "", max(0, width - title_width - 33))
        rendered.append(
            _fit_ansi(
                f"{marker} {index:<2} {_fit_ansi(title, title_width)} "
                f"{_fit_ansi(state, 10)} {_fit_ansi(worker, 10)} {kind}",
                width,
            )
        )
        outputs = (job_artifacts or {}).get(item_id) or []
        if show_outputs and outputs:
            latest = outputs[0]
            counts = (job_counts or {}).get(item_id) or {}
            output_total = int(counts.get("artifacts") or len(outputs))
            count_prefix = f"{_muted(str(output_total) + 'x')} " if output_total > 1 else ""
            output_title = _one_line(
                str(latest.get("title") or latest.get("id") or "saved output"),
                max(8, width - 14 - len(f"{output_total}x " if output_total > 1 else "")),
            )
            rendered.append(_fit_ansi(f"   {_event_badge('SAVE')} {count_prefix}{output_title}", width))
        if show_outputs:
            outcome_line = _job_latest_non_output_line(
                (job_summary_events or {}).get(item_id) or [],
                width=width,
                skip_save=bool(outputs),
            )
            if outcome_line:
                rendered.append(outcome_line)
    return rendered


def _job_latest_non_output_line(events: list[dict[str, Any]], *, width: int, skip_save: bool) -> str:
    for event in reversed(events):
        parsed = model_update_event_parts(event, width=max(width, 120))
        if not parsed:
            continue
        label, text, _clock = parsed
        if label == "DONE":
            continue
        if skip_save and label == "SAVE":
            continue
        prefix = f"   {_event_badge(label)} "
        return _fit_ansi(prefix + _one_line(text, max(12, width - len(_strip_ansi(prefix)))), width)
    return ""


def _defer_status_line(job: dict[str, Any], *, width: int) -> str:
    until = job_deferred_until(job)
    if not until:
        return ""
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    reason = str(metadata.get("defer_reason") or metadata.get("defer_next_action") or "").strip()
    time_text = until.astimezone().strftime("%b %d %H:%M")
    detail = f"next check {time_text}"
    if reason:
        detail += f" - {reason}"
    return _fit_ansi(f"{_muted('Wait')}   {_one_line(detail, max(12, width - 7))}", width)


def _rank_visible_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_order = {"active": 0, "open": 1, "blocked": 2, "validating": 3, "done": 4, "skipped": 5}
    return sorted(
        [task for task in tasks if isinstance(task, dict)],
        key=lambda task: (
            status_order.get(str(task.get("status") or "open"), 9),
            -int(task.get("priority") or 0),
            str(task.get("title") or ""),
        ),
    )


def _chat_workspace_lines(
    *,
    right_view: str,
    job: dict[str, Any],
    state: str,
    worker: str,
    daemon_text: str,
    goal_text: str,
    latest_text: str,
    metrics: list[tuple[str, Any]],
    token_usage: dict[str, Any],
    context_length: int,
    width: int,
) -> list[str]:
    goal_lines = textwrap.wrap(goal_text, width=max(20, width - 8))[:2] or [""]
    while len(goal_lines) < 2:
        goal_lines.append("")
    title = _one_line(str(job.get("title") or "untitled"), max(10, width))
    lines = [
        f"{_muted('Page')}   {_page_indicator(right_view, CHAT_RIGHT_PAGES)}",
        _bold(title),
        f"{_status_badge(state)} {_muted('worker')} {_status_badge(worker)}  {_muted(_one_line(daemon_text, max(8, width - 28)))}",
        f"{_muted('Goal')}   {goal_lines[0]}",
        f"{_muted('       ')}{goal_lines[1]}",
        f"{_muted('Latest')} {_one_line(latest_text, width - 8)}",
    ]
    task_line = _current_task_line(job, width=width)
    if task_line:
        lines.append(task_line)
    context_line = _context_pressure_line(token_usage, context_length=context_length, width=width)
    if context_line:
        lines.append(context_line)
    lines.extend(_metrics_grid_lines(metrics, width=width))
    return lines


def _current_task_line(job: dict[str, Any], *, width: int) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    visible = [
        task
        for task in tasks
        if isinstance(task, dict)
        and str(task.get("status") or "open") in {"active", "open", "blocked"}
    ]
    if not visible:
        return ""
    ranked = _rank_visible_tasks(visible)
    task = ranked[0]
    status = str(task.get("status") or "open")
    title = _one_line(str(task.get("title") or "task"), max(12, width - 16))
    return _fit_ansi(f"{_muted('Task')}   {_status_badge(status)} {title}", width)


def _context_pressure_line(usage: dict[str, Any], *, context_length: int, width: int) -> str:
    latest_prompt = _safe_int(usage.get("latest_prompt_tokens"))
    context_limit = _safe_int(usage.get("latest_context_length")) or context_length
    if latest_prompt <= 0 or context_limit <= 0:
        return ""
    fraction = latest_prompt / max(1, context_limit)
    if fraction < 0.65:
        return ""
    label = "high" if fraction >= 0.85 else "watch" if fraction >= 0.65 else "ok"
    detail = (
        f"{_format_compact_count(latest_prompt)}/{_format_compact_count(context_limit)} "
        f"{fraction:.0%} {label}"
    )
    return _fit_ansi(f"{_muted('Context')} {_one_line(detail, max(12, width - 8))}", width)


def _metrics_grid_lines(metrics: list[tuple[str, Any]], *, width: int) -> list[str]:
    wanted = ["actions", "outputs", "findings", "sources", "tasks", "experiments", "memory"]
    lookup = {label: value for label, value in metrics}
    items = [(label, lookup[label]) for label in wanted if label in lookup]
    if width < 40:
        return [_metric_strip(items, width=width)]
    lines: list[str] = []
    col_width = max(16, (width - 2) // 2)
    for index in range(0, len(items), 2):
        left = _metric_cell(items[index], width=col_width)
        right = _metric_cell(items[index + 1], width=col_width) if index + 1 < len(items) else ""
        lines.append(_fit_ansi(left + "  " + right, width))
    return lines


def _metric_cell(item: tuple[str, Any], *, width: int) -> str:
    label, value = item
    return _fit_ansi(f"{_muted(label)} {_bold(value)}", width)


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
