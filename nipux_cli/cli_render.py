"""Reusable text renderers for non-frame CLI commands."""

from __future__ import annotations

import json
import os
import shutil
import textwrap
from pathlib import Path
from typing import Any

from nipux_cli.event_render import event_display_parts
from nipux_cli.tui_event_format import clean_step_summary
from nipux_cli.tui_status import job_display_state, worker_label
from nipux_cli.tui_style import _accent, _event_badge, _fancy_ui, _muted, _one_line, _status_badge


def clip_json(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated {len(text) - limit} chars"


def print_step(step: dict[str, Any], *, verbose: bool = False, chars: int = 4000) -> None:
    tool = step.get("tool_name") or "-"
    summary = _one_line(clean_step_summary(step.get("summary") or ""), chars)
    error = _one_line(step["error"], chars) if step.get("error") else ""
    print(f"step #{step['step_no']} {step['started_at']} {step['status']} {step['kind']} {tool}")
    if summary:
        print(f"  summary: {summary}")
    if error:
        print(f"  error: {error}")
    output_data = step.get("output") or {}
    if not verbose and isinstance(output_data, dict):
        artifact_id = output_data.get("artifact_id")
        if artifact_id:
            print(f"  artifact: {artifact_id} (view with: artifact {artifact_id})")
        lesson = output_data.get("lesson") if isinstance(output_data.get("lesson"), dict) else None
        if lesson:
            print(f"  lesson: {_one_line(lesson.get('lesson') or '', chars)}")
        update = output_data.get("update") if isinstance(output_data.get("update"), dict) else None
        if update:
            print(f"  update: {_one_line(update.get('message') or '', chars)}")
        source = output_data.get("source") if isinstance(output_data.get("source"), dict) else None
        if source:
            print(f"  source: {_one_line(source.get('source') or '', chars)} score={source.get('usefulness_score')}")
        if isinstance(output_data.get("findings"), list):
            print(f"  findings: {output_data.get('added', 0)} new, {output_data.get('updated', 0)} updated")
        checkpoint = output_data.get("auto_checkpoint") if isinstance(output_data.get("auto_checkpoint"), dict) else None
        if checkpoint:
            print(f"  auto checkpoint: {checkpoint.get('artifact_id')}")
    if verbose:
        input_data = step.get("input") or {}
        if input_data:
            print("  input:")
            print(clip_json(input_data, chars))
        if output_data:
            print("  output:")
            print(clip_json(output_data, chars))


def print_artifact(artifact: dict[str, Any]) -> None:
    title = artifact.get("title") or artifact["id"]
    print(f"artifact {artifact['created_at']} {artifact['type']} {title}")
    print(f"  {artifact['path']}")


def print_run(run: dict[str, Any]) -> None:
    print(f"run {run['started_at']} {run['status']} {run['id']} {run.get('model') or ''}")
    if run.get("error"):
        print(f"  error: {run['error']}")


def print_wrapped(prefix: str, text: Any, *, width: int, subsequent_indent: str = "") -> None:
    content = " ".join(str(text).split())
    if not content:
        print(prefix.rstrip())
        return
    available = max(20, min(width, 96) - len(prefix))
    wrapped = textwrap.wrap(content, width=available) or [content]
    print(prefix + wrapped[0])
    for line in wrapped[1:]:
        print(subsequent_indent + line)


def section_title(title: str, subtitle: str = "") -> str:
    text = title.upper()
    if subtitle:
        text = f"{text} - {_one_line(subtitle, 52)}"
    width = min(terminal_width(), 96)
    if len(text) >= width - 2:
        return text[:width]
    if _fancy_ui():
        return _accent(f"╭─ {text} " + "─" * max(0, width - len(text) - 4))
    return f"{text} " + "-" * max(0, width - len(text) - 1)


def print_metric_grid(items: list[tuple[str, Any]]) -> None:
    width = min(terminal_width(), 96)
    cell_width = 24 if width >= 80 else 18
    cells = [f"{label:<12} {value}"[:cell_width].ljust(cell_width) for label, value in items]
    columns = max(1, width // cell_width)
    for start in range(0, len(cells), columns):
        print("  " + "  ".join(cells[start : start + columns]).rstrip())


def short_path(path: Path | str, *, max_width: int = 80) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        text = "~" + text[len(home) :]
    if len(text) <= max_width:
        return text
    keep = max(12, max_width - 4)
    return "..." + text[-keep:]


def print_jobs_panel(jobs: list[dict[str, Any]], *, focused_job_id: str, daemon_running: bool) -> None:
    print(section_title("Jobs"))
    if not jobs:
        print("  No jobs yet. Type an objective or use /new OBJECTIVE.")
        return
    print("  #  job                         state       worker      kind")
    for index, item in enumerate(jobs[:8], start=1):
        marker = "*" if str(item.get("id")) == focused_job_id else " "
        state = job_display_state(item, daemon_running)
        worker = worker_label(item, daemon_running)
        title = _one_line(item.get("title") or item.get("id") or "job", 27)
        print(f"  {marker}{index:<2} {title:<27} {_status_badge(state):<11} {_status_badge(worker):<11} {item.get('kind') or ''}")
    if len(jobs) > 8:
        print(f"  ... {len(jobs) - 8} more. Use /jobs for the full list.")
    print("  switch: /focus JOB_TITLE")


def next_operator_action(job: dict[str, Any], daemon_running: bool) -> str:
    status = str(job.get("status") or "")
    if status == "planning":
        return "review the plan, or run when ready"
    if status == "cancelled":
        return "resume to reopen this job, or delete it"
    if status == "paused":
        return "resume, then run to continue"
    if status in {"queued", "running"} and not daemon_running:
        return "run to start background work"
    if status in {"queued", "running"} and daemon_running:
        return "daemon is active; live steps will stream here"
    if status == "completed":
        return "inspect history or artifacts"
    if status == "failed":
        return "resume, then run one worker step to test recovery"
    return ""


def important_startup_events(events: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if len(events) <= limit:
        return events
    important_types = {
        "operator_message",
        "agent_message",
        "artifact",
        "finding",
        "task",
        "experiment",
        "lesson",
        "reflection",
        "error",
        "compaction",
    }
    selected: list[dict[str, Any]] = []
    for event in reversed(events):
        if event.get("event_type") in important_types:
            selected.append(event)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for event in reversed(events):
            if event not in selected:
                selected.append(event)
            if len(selected) >= limit:
                break
    selected.sort(key=lambda event: (str(event.get("created_at") or ""), str(event.get("id") or "")))
    return selected


def print_event_card(event: dict[str, Any], *, chars: int, artifact_indexes: dict[str, int] | None = None) -> None:
    when, label, detail, access = event_display_parts(event, chars=chars, full=False)
    artifact_indexes = artifact_indexes or {}
    artifact_index = artifact_indexes.get(str(event.get("ref_id") or ""))
    if artifact_index and event.get("event_type") == "artifact":
        access = f"open: /artifact {artifact_index}"
    print(f"  {_event_badge(label):<8} {_muted(when):<16} {_one_line(detail, chars)}")
    if access:
        print(f"  {'':<8} {'':<16} {access}")


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    public = dict(event)
    public.pop("metadata_json", None)
    return public


def print_event_details(event: dict[str, Any], *, chars: int) -> None:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if not metadata:
        return
    compact = {
        key: value
        for key, value in metadata.items()
        if key not in {"input", "output"} and value not in (None, "", [], {})
    }
    if compact:
        print(f"     meta: {_one_line(json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str), chars)}")
    if isinstance(metadata.get("input"), dict):
        print(f"     input: {_one_line(json.dumps(metadata['input'], ensure_ascii=False, sort_keys=True, default=str), chars)}")
    if isinstance(metadata.get("output"), dict):
        print(f"     output: {_one_line(json.dumps(metadata['output'], ensure_ascii=False, sort_keys=True, default=str), chars)}")


def step_line(step: dict[str, Any], *, chars: int = 180) -> str:
    tool = step.get("tool_name") or step.get("kind") or "-"
    summary = clean_step_summary(step.get("summary") or step.get("error") or "-")
    error = " ERROR" if step.get("error") else ""
    return f"#{step['step_no']:<4} {step['status']:<9} {tool:<18} {_one_line(summary, chars)}{error}"


def terminal_width() -> int:
    return shutil.get_terminal_size((120, 40)).columns


def rule(char: str = "-", width: int | None = None) -> str:
    return char * min(width or terminal_width(), 96)


def json_default(value: Any) -> str:
    return str(value)


def daemon_state_line(lock: dict[str, Any]) -> str:
    metadata = lock.get("metadata") if isinstance(lock.get("metadata"), dict) else {}
    if lock.get("running"):
        pid = metadata.get("pid") or "unknown"
        stale = " stale-runtime" if lock.get("stale") else ""
        return f"running pid={pid}{stale}"
    return "ready when work starts"


def daemon_event_line(event: dict[str, Any], *, chars: int, job_titles: dict[str, str] | None = None) -> str:
    at = str(event.get("at") or "?")
    name = str(event.get("event") or "?")
    pieces = []
    job_titles = job_titles or {}
    for key in ("status", "tool", "job_id", "step_id", "error_type", "detail", "error"):
        value = event.get(key)
        if value not in (None, ""):
            label = key
            if key == "job_id":
                value = job_titles.get(str(value), value)
            pieces.append(f"{label}={value}")
    suffix = " ".join(pieces)
    return _one_line(f"{at} {name} {suffix}".strip(), chars)


def job_ref_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text or None


def note_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value).strip()
    return str(value).strip()
