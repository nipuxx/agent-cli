"""Chat workspace terminal frame rendering."""

from __future__ import annotations

from typing import Any

from nipux_cli.config import load_config
from nipux_cli.first_run_tui import first_run_themed_lines
from nipux_cli.settings import edit_target_hint, edit_target_label, edit_target_masks_input
from nipux_cli.tui_commands import CHAT_SLASH_COMMANDS, slash_suggestion_lines
from nipux_cli.tui_event_format import clean_step_summary
from nipux_cli.tui_events import chat_pane_lines
from nipux_cli.tui_layout import _compose_bar, _top_bar
from nipux_cli.tui_outcomes import chat_updates_pane_lines
from nipux_cli.tui_status import (
    chat_work_pane_lines,
    job_display_state,
    right_pane_lines,
    worker_label,
)
from nipux_cli.tui_style import _accent, _bold, _fit_ansi, _muted, _one_line, _strip_ansi


def build_chat_frame(
    snapshot: dict[str, Any],
    input_buffer: str,
    notices: list[str],
    *,
    width: int,
    height: int,
    right_view: str = "status",
    selected_control: int = 0,
    editing_field: str | None = None,
    modal_view: str | None = None,
) -> str:
    del selected_control
    width = max(92, width)
    height = max(22, height)
    job = snapshot["job"]
    jobs = snapshot["jobs"]
    steps = snapshot["steps"]
    artifacts = snapshot["artifacts"]
    job_id = str(snapshot["job_id"])
    job_artifacts = snapshot.get("job_artifacts") if isinstance(snapshot.get("job_artifacts"), dict) else {}
    if artifacts:
        job_artifacts.setdefault(job_id, artifacts)
    job_summary_events = snapshot.get("job_summary_events") if isinstance(snapshot.get("job_summary_events"), dict) else {}
    job_counts = snapshot.get("job_counts") if isinstance(snapshot.get("job_counts"), dict) else {}
    memory_entries = snapshot["memory_entries"]
    events = snapshot["events"]
    summary_events = snapshot.get("summary_events") if isinstance(snapshot.get("summary_events"), list) else events
    daemon = snapshot["daemon"]
    model = str(snapshot["model"])
    base_url = str(snapshot.get("base_url") or "")
    token_usage = snapshot.get("token_usage") if isinstance(snapshot.get("token_usage"), dict) else {}
    context_length = int(snapshot.get("context_length") or 0)
    counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {}
    findings = _metadata_records(job, "finding_ledger")
    sources = _metadata_records(job, "source_ledger")
    tasks = _metadata_records(job, "task_queue")
    experiments = _metadata_records(job, "experiment_ledger")
    lessons = _metadata_records(job, "lessons")
    roadmap = job.get("metadata", {}).get("roadmap") if isinstance(job.get("metadata"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap, dict) and isinstance(roadmap.get("milestones"), list) else []
    open_tasks = sum(1 for task in tasks if str(task.get("status") or "open") in {"open", "active"})
    state = job_display_state(job, bool(daemon["running"]))
    worker = worker_label(job, bool(daemon["running"]))
    latest_step = steps[-1] if steps else None
    right_width = min(max(50, int(width * 0.34)), 72)
    left_width = max(48, width - right_width - 3)
    if left_width < 48:
        left_width = 48
        right_width = max(34, width - left_width - 3)
    latest_text = _step_line(latest_step, chars=right_width - 6) if latest_step else "no worker steps yet"
    daemon_text = _daemon_state_line(daemon)
    goal_text = " ".join(str(job.get("objective") or "").split())
    metrics = [
        ("actions", counts.get("steps", _step_count(steps))),
        ("outputs", counts.get("artifacts", len(artifacts))),
        ("findings", len(findings)),
        ("sources", len(sources)),
        ("tasks", f"{len(tasks)}/{open_tasks} open"),
        ("roadmap", len(milestones)),
        ("experiments", len(experiments)),
        ("lessons", len(lessons)),
        ("memory", counts.get("memory", len(memory_entries))),
    ]

    header = _top_bar(
        width,
        state=state,
        daemon=daemon_text,
        model=model,
        token_usage=token_usage,
        context_length=context_length,
        base_url=base_url,
    )
    if editing_field:
        hint = edit_target_hint(editing_field)
        prompt_label = edit_target_label(editing_field)
    else:
        hint = "Talk normally  ·  /new goal starts work  ·  /run resumes  ·  ←→ pages  ·  ↑↓ jobs"
        prompt_label = "❯"
    suggestions = [] if editing_field else slash_suggestion_lines(input_buffer, CHAT_SLASH_COMMANDS, width=width)
    compose_lines = _compose_bar(
        input_buffer,
        width=width,
        hint=hint,
        suggestions=suggestions,
        prompt_label=prompt_label,
        mask_input=edit_target_masks_input(editing_field),
    )
    footer_rows = len(compose_lines)
    body_rows = max(10, height - len(header) - 1 - footer_rows)
    chat_lines = chat_pane_lines(events, notices, width=left_width, rows=body_rows)
    if right_view == "updates":
        right_lines = chat_updates_pane_lines(
            job=job,
            events=summary_events,
            width=right_width,
            rows=body_rows,
        )
        right_title = "Outcomes"
    elif right_view == "work":
        right_lines = chat_work_pane_lines(
            job=job,
            events=events,
            tasks=tasks,
            experiments=experiments,
            width=right_width,
            rows=body_rows,
        )
        right_title = "Worker"
    else:
        right_lines = right_pane_lines(
            job=job,
            jobs=jobs,
            job_artifacts=job_artifacts,
            job_summary_events=job_summary_events,
            job_counts=job_counts,
            job_id=job_id,
            daemon_running=bool(daemon["running"]),
            state=state,
            worker=worker,
            daemon_text=daemon_text,
            model=model,
            goal_text=goal_text,
            latest_text=latest_text,
            metrics=metrics,
            events=summary_events,
            token_usage=token_usage,
            context_length=context_length,
            width=right_width,
            rows=body_rows,
            right_view=right_view,
        )
        right_title = "Jobs"
    lines = [*header, _two_col_title(left_width, right_width, "Conversation", right_title)]
    for index in range(body_rows):
        left = chat_lines[index] if index < len(chat_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        lines.append(_two_col_line(left, right, left_width=left_width, right_width=right_width))
    lines.extend(compose_lines)
    if len(lines) > height:
        keep_top = min(4, len(header) + 1)
        keep_bottom = footer_rows
        middle_budget = max(0, height - keep_top - keep_bottom)
        lines = lines[:keep_top] + lines[-(middle_budget + keep_bottom) : -keep_bottom] + lines[-keep_bottom:]
    if modal_view == "settings":
        lines = _overlay_settings_modal(lines[:height], width=width, height=height)
    return "\n".join(first_run_themed_lines(lines[:height], width=width))


def _two_col_title(left_width: int, right_width: int, left: str, right: str) -> str:
    return _fit_ansi(_bold(left.upper()), left_width) + _muted(" │ ") + _fit_ansi(_bold(right.upper()), right_width)


def _two_col_line(left: str, right: str, *, left_width: int, right_width: int) -> str:
    return _fit_ansi(left, left_width) + _muted(" │ ") + _fit_ansi(right, right_width)


def _overlay_settings_modal(lines: list[str], *, width: int, height: int) -> list[str]:
    config = load_config()
    key_state = "set" if config.model.api_key else "missing"
    input_cost = _rate_text(config.model.input_cost_per_million)
    output_cost = _rate_text(config.model.output_cost_per_million)
    content = [
        _bold("Model"),
        _settings_row("id", config.model.model, "/model MODEL"),
        _settings_row("endpoint", config.model.base_url, "/base-url URL"),
        _settings_row("key", f"{key_state} in {config.model.api_key_env}", "/api-key KEY"),
        _settings_row(
            "limits",
            f"context {config.model.context_length}, timeout {config.model.request_timeout_seconds:g}s",
            "/context TOKENS /timeout SECONDS",
        ),
        "",
        _bold("Runtime"),
        _settings_row("home", str(config.runtime.home), "/home PATH"),
        _settings_row(
            "steps",
            f"tool {config.runtime.max_step_seconds}s, preview {config.runtime.artifact_inline_char_limit} chars",
            "/step-limit SECONDS /output-chars CHARS",
        ),
        _settings_row(
            "digest",
            f"{config.runtime.daily_digest_enabled} at {config.runtime.daily_digest_time}",
            "/daily-digest BOOL /digest-time HH:MM",
        ),
        "",
        _bold("Cost"),
        _settings_row("rates", f"input {input_cost}, output {output_cost}", "/input-cost DOLLARS /output-cost DOLLARS"),
        "",
        _muted("Edit with slash commands in the composer. Esc closes."),
    ]
    box_width = min(max(64, int(width * 0.58)), width - 8)
    box_height = min(len(content) + 4, height - 6)
    inner = max(20, box_width - 4)
    title = f" Settings {_accent('●')} "
    rule_width = max(2, box_width - len(_strip_ansi(title)) - 2)
    left_rule = max(1, rule_width // 2)
    right_rule = max(1, rule_width - left_rule)
    top = "╭" + "─" * left_rule + title + "─" * right_rule + "╮"
    box = [top]
    for item in content[: box_height - 3]:
        if item:
            box.append("│ " + _fit_ansi(item, inner) + " │")
        else:
            box.append("│ " + " " * inner + " │")
    while len(box) < box_height - 1:
        box.append("│ " + " " * inner + " │")
    box.append("╰" + "─" * (box_width - 2) + "╯")
    output = [_fit_ansi(line, width) for line in lines]
    start_y = max(2, (height - len(box)) // 2)
    start_x = max(0, (width - box_width) // 2)
    for offset, modal_line in enumerate(box):
        target = start_y + offset
        if target >= len(output):
            break
        output[target] = _fit_ansi(" " * start_x + modal_line, width)
    return output


def _settings_row(label: str, value: Any, command: str) -> str:
    value_text = _one_line(value, 42)
    return f"{_muted(label.ljust(9))} {_bold(value_text)}  {_muted(command)}"


def _rate_text(value: float | None) -> str:
    return "provider-reported" if value is None else f"${value:g}/1M"


def _metadata_records(job: dict[str, Any], key: str) -> list[dict[str, Any]]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    values = metadata.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _step_count(steps: list[dict[str, Any]]) -> int:
    numbers = [int(step.get("step_no") or 0) for step in steps]
    return max(numbers, default=0)


def _step_line(step: dict[str, Any], *, chars: int = 180) -> str:
    tool = step.get("tool_name") or step.get("kind") or "-"
    summary = clean_step_summary(step.get("summary") or step.get("error") or "-")
    error = " ERROR" if step.get("error") else ""
    return f"#{step['step_no']:<4} {step['status']:<9} {tool:<18} {_one_line(summary, chars)}{error}"


def _daemon_state_line(lock: dict[str, Any]) -> str:
    metadata = lock.get("metadata") if isinstance(lock.get("metadata"), dict) else {}
    if lock.get("running"):
        pid = metadata.get("pid") or "unknown"
        stale = " stale-runtime" if lock.get("stale") else ""
        return f"running pid={pid}{stale}"
    return "ready when work starts"
