"""Compact event rendering helpers for the Nipux terminal UI."""

from __future__ import annotations

import os
import re
import shlex
import textwrap
from pathlib import Path
from typing import Any

from nipux_cli.tui_style import _bold, _event_badge, _fit_ansi, _muted, _one_line, _page_indicator, _strip_ansi, _style


CHAT_RIGHT_PAGES = [("status", "Status"), ("updates", "Updates"), ("work", "Work")]

LOW_SIGNAL_FRAME_TOOLS = {
    "acknowledge_operator_context",
    "read_artifact",
    "record_experiment",
    "record_findings",
    "record_lesson",
    "record_milestone_validation",
    "record_roadmap",
    "record_source",
    "record_tasks",
    "reflect",
    "report_update",
    "search_artifacts",
    "update_job_state",
    "write_artifact",
}


def chat_event_parts(event: dict[str, Any]) -> tuple[str, str, str] | None:
    kind = str(event.get("event_type") or "")
    title = str(event.get("title") or "").strip()
    body = str(event.get("body") or "")
    clock = event_clock(event)
    if kind == "operator_message":
        return "YOU", body, clock
    if kind == "agent_message" and title == "chat":
        return "AGENT", body, clock
    return None


def append_chat_output(lines: list[str], label: str, body: Any, *, clock: str, width: int) -> None:
    label_text = _fit_ansi(_event_badge(label), 8)
    clock_text = _fit_ansi(_muted(clock), 5) if clock else " " * 5
    prefix = f"{clock_text} {label_text} "
    prefix_width = 15
    available = max(18, width - prefix_width)
    first = True
    for paragraph in _chat_message_paragraphs(body):
        wrapped = textwrap.wrap(paragraph, width=available) or [""]
        for part in wrapped:
            if first:
                lines.append(_fit_ansi(prefix + part, width))
                first = False
            else:
                lines.append(_fit_ansi(" " * prefix_width + part, width))
    if first:
        lines.append(_fit_ansi(prefix, width))


def worker_activity_lines(events: list[dict[str, Any]], *, width: int, limit: int) -> list[str]:
    items: list[dict[str, Any]] = []
    for event in events:
        line = minimal_live_event_line(event, chars=max(16, width - 12))
        if not line:
            continue
        if items and items[-1].get("key") == line:
            items[-1]["count"] = int(items[-1].get("count") or 1) + 1
            continue
        items.append({"line": line, "count": 1, "key": line})
    rendered = []
    for item in items[-limit:]:
        line = str(item["line"])
        count = int(item.get("count") or 1)
        if count > 1:
            line = f"x{count} {line}"
        rendered.append(f"{live_badge(line)} {_one_line(line, max(16, width - 9))}")
    return rendered


def model_update_event_parts(event: dict[str, Any], *, width: int) -> tuple[str, str, str] | None:
    kind = str(event.get("event_type") or "")
    title = generic_display_text(event.get("title") or "")
    body = generic_display_text(event.get("body") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    status = str(metadata.get("status") or "")
    clock = event_clock(event)
    chars = max(24, width - 16)
    if kind == "artifact":
        detail = event_title_body(title, body or str(metadata.get("summary") or ""), fallback="saved output")
        return "SAVE", _one_line(detail, chars), clock
    if kind == "finding":
        return "FIND", _one_line(event_title_body(title, body, fallback="finding"), chars), clock
    if kind == "source":
        return "SOURCE", _one_line(event_title_body(title, body, fallback="source"), chars), clock
    if kind == "experiment":
        metric = experiment_metric_text(metadata)
        detail = event_title_body(title, body, fallback="measurement")
        if metric and metric not in detail:
            detail = f"{detail} - {metric}"
        return "TEST", _one_line(detail, chars), clock
    if kind == "task":
        task_status = str(metadata.get("status") or "")
        detail = event_title_body(title, body, fallback="task")
        prefix = f"{task_status} " if task_status else ""
        return "TASK", _one_line(prefix + detail, chars), clock
    if kind == "roadmap":
        return "ROAD", _one_line(event_title_body(title, body, fallback="roadmap"), chars), clock
    if kind == "milestone_validation":
        validation = str(metadata.get("validation_status") or metadata.get("status") or "")
        detail = event_title_body(title, body, fallback="milestone")
        return "VALID", _one_line(f"{validation} {detail}".strip(), chars), clock
    if kind == "lesson":
        return "LEARN", _one_line(event_title_body(title, body, fallback="lesson"), chars), clock
    if kind == "reflection":
        return "PLAN", _one_line(brief_reflection_text(body or title), chars), clock
    if kind == "agent_message" and title.lower() in {"error", "blocked"}:
        detail = _chat_agent_message_text(title, body) or event_title_body(title, body, fallback="error")
        return "FAIL", _one_line(detail, chars), clock
    if kind == "agent_message" and title.lower() in {"progress", "update", "report", "plan", "planning"}:
        detail = _chat_agent_message_text(title, body) or event_title_body(title, body, fallback="update")
        return "UPDATE", _one_line(detail, chars), clock
    if kind == "tool_result" and status == "completed":
        tool = title
        if tool in {"web_search", "web_extract"}:
            return "DONE", _one_line(tool_live_summary(tool, metadata, body), chars), clock
        if tool == "shell_exec":
            command = str(event_tool_args(metadata).get("command") or "")
            target = shell_write_target(command)
            if target:
                return "FILE", _one_line(f"updated {_short_path(target, max_width=chars - 8)} via shell", chars), clock
        if tool == "write_file":
            output = metadata.get("output") if isinstance(metadata.get("output"), dict) else {}
            path = str(output.get("path") or event_tool_args(metadata).get("path") or "")
            return "FILE", _one_line(f"updated {_short_path(path, max_width=chars - 8)}", chars), clock
    return None


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
        prefix = f"{_muted('Outcome')} {_event_badge(label)} "
        return _fit_ansi(prefix + _one_line(text, max(12, width - len(_strip_ansi(prefix)))), width)
    if fallback:
        label, text, _clock = fallback
        prefix = f"{_muted('Outcome')} {_event_badge(label)} "
        return _fit_ansi(prefix + _one_line(text, max(12, width - len(_strip_ansi(prefix)))), width)
    return ""


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
        _bold("Progress by hour"),
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
        parsed = model_update_event_parts(event, width=max(width, 220))
        if not parsed:
            continue
        label, text, clock = parsed
        hour = _event_hour(event)
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
    recent_hours = order[-max(1, min(len(order), limit)) :]
    per_bucket = max(2, min(8, (limit // max(1, len(recent_hours))) - 1))
    for hour in recent_hours:
        bucket = buckets[hour]
        counts = bucket["counts"]
        summary = " ".join(f"{count} {label.lower()}" for label, count in sorted(counts.items()))
        rendered.append(_fit_ansi(f"{_muted(hour)} {_bold(summary or 'activity')}", width))
        for label, text in bucket["items"][-per_bucket:]:
            prefix = f"  {_event_badge(label)} "
            available = max(16, width - len(_strip_ansi(prefix)))
            parts = textwrap.wrap(text, width=available) or [""]
            rendered.append(_fit_ansi(prefix + parts[0], width))
            for part in parts[1:3]:
                rendered.append(_fit_ansi(" " * len(_strip_ansi(prefix)) + part, width))
                if len(rendered) >= limit:
                    return rendered[:limit]
        if len(rendered) >= limit:
            return rendered[:limit]
    return rendered[-limit:]


def minimal_live_event_line(event: dict[str, Any], *, chars: int = 92) -> str:
    kind = str(event.get("event_type") or "")
    title = str(event.get("title") or "").strip()
    body = generic_display_text(event.get("body") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    status = str(metadata.get("status") or "")
    if kind == "operator_message":
        return ""
    if kind == "agent_message" and title == "chat":
        return ""
    if kind == "operator_context":
        return _one_line(f"operator {title or body}", chars)
    if kind == "tool_call":
        if title in LOW_SIGNAL_FRAME_TOOLS:
            return ""
        return _one_line("start " + tool_live_summary(title, metadata, body), chars)
    if kind == "tool_result":
        if title in LOW_SIGNAL_FRAME_TOOLS and status == "completed":
            return ""
        if title == "llm" and status == "failed":
            return ""
        prefix = "blocked" if status == "blocked" else "failed" if status == "failed" else "done"
        detail = friendly_error_text(body or title) if status in {"blocked", "failed"} else tool_live_summary(title, metadata, body)
        return _one_line(f"{prefix} {detail}", chars)
    if kind == "error":
        detail = friendly_error_text(body or title or "error")
        return _one_line(f"error {detail}", chars)
    if kind == "artifact":
        return _one_line(f"saved {title or body or 'output'}", chars)
    if kind == "finding":
        return _one_line(f"finding {title or body}", chars)
    if kind == "source":
        return _one_line(f"source {title or body}", chars)
    if kind == "task":
        return _one_line(f"task {title or body}", chars)
    if kind == "roadmap":
        return _one_line(f"roadmap {title or body}", chars)
    if kind == "milestone_validation":
        validation = str(metadata.get("validation_status") or metadata.get("status") or "")
        return _one_line(f"validate {validation} {title or body}".strip(), chars)
    if kind == "experiment":
        return _one_line(f"experiment {title or body}", chars)
    if kind == "lesson":
        detail = event_title_body(title, body, fallback="lesson")
        return _one_line(f"learned {detail}", chars)
    if kind == "reflection":
        return _one_line(f"reflect {brief_reflection_text(body or title)}", chars)
    if kind == "agent_message":
        if title not in {"chat", "progress", "update", "report"}:
            return ""
        return _one_line(f"update {body or title}", chars)
    if kind in {"daemon", "loop"}:
        return ""
    return ""


def live_badge(text: str) -> str:
    badge_text = re.sub(r"^x[0-9]+\s+", "", text)
    if badge_text.startswith("error") or badge_text.startswith("failed"):
        return _style("FAIL", "31")
    if badge_text.startswith("blocked"):
        return _style("BLOCK", "33")
    if badge_text.startswith("start"):
        return _style("run ", "36")
    if badge_text.startswith("done"):
        return _style("done", "32")
    if badge_text.startswith("saved"):
        return _style("save", "32")
    if badge_text.startswith("task"):
        return _style("task", "33")
    if badge_text.startswith("learned"):
        return _style("mem ", "36")
    if badge_text.startswith("reflect"):
        return _style("plan", "35")
    if badge_text.startswith("update"):
        return _style("note", "35")
    return _style("info", "2")


def tool_live_summary(tool: str, metadata: dict[str, Any], body: str) -> str:
    args = event_tool_args(metadata)
    clean_body = clean_step_summary(body)
    if tool == "web_search":
        query = str(args.get("query") or _regex_group(r"query='([^']+)'", clean_body) or "")
        return f"search {query}" if query else "search web"
    if tool == "web_extract":
        urls = args.get("urls") if isinstance(args.get("urls"), list) else []
        count = len(urls)
        fetched = _regex_group(r"fetched ([0-9]+/[0-9]+ pages)", clean_body)
        return f"extract {fetched or (str(count) + ' pages' if count else 'pages')}"
    if tool == "shell_exec":
        command = str(args.get("command") or _regex_group(r"cmd='([^']+)'", clean_body) or "")
        prefix = f"shell {_short_command(command)}" if command else "shell command"
        rc = metadata.get("output", {}).get("returncode") if isinstance(metadata.get("output"), dict) else None
        return f"{prefix} rc={rc}" if rc is not None else prefix
    if tool == "browser_navigate":
        url = str(args.get("url") or _regex_group(r"<([^>]+)>", clean_body) or "")
        return f"open {_short_url(url)}" if url else "open page"
    if tool == "browser_snapshot":
        return "snapshot page"
    if tool == "browser_click":
        ref = str(args.get("ref") or "")
        return f"click {ref}" if ref else "click page"
    if tool == "browser_scroll":
        return f"scroll {args.get('direction') or 'page'}"
    if tool == "write_artifact":
        return "save output"
    if tool == "write_file":
        args_path = str(args.get("path") or "")
        output = metadata.get("output") if isinstance(metadata.get("output"), dict) else {}
        path = str(output.get("path") or args_path)
        return f"update {_short_path(path, max_width=36)}" if path else "update file"
    if tool == "record_lesson":
        return "learn memory"
    if tool == "record_source":
        return "score source"
    if tool == "record_findings":
        return "record findings"
    if tool == "record_tasks":
        return "update tasks"
    if tool == "record_roadmap":
        return "update roadmap"
    if tool == "record_milestone_validation":
        return "validate roadmap"
    if tool == "record_experiment":
        return "record experiment"
    if tool == "acknowledge_operator_context":
        return "ack operator"
    if tool == "report_update":
        return "report update"
    if tool == "read_artifact":
        return "read output"
    if tool == "search_artifacts":
        return "search outputs"
    return tool or clean_body or "step"


def event_tool_args(metadata: dict[str, Any]) -> dict[str, Any]:
    input_data = metadata.get("input") if isinstance(metadata.get("input"), dict) else {}
    args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
    return args


def shell_write_target(command: str) -> str:
    if not command.strip():
        return ""
    redirect = re.search(r"(?:^|\s)(?:1?>|>>)\s*([^\s;&|]+)", command)
    if redirect:
        target = redirect.group(1).strip("'\"")
        if target and not target.startswith("&"):
            return target
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for index, part in enumerate(parts):
        if part != "tee":
            continue
        for candidate in parts[index + 1 :]:
            if candidate.startswith("-"):
                continue
            return candidate
    return ""


def event_title_body(title: str, body: str, *, fallback: str) -> str:
    if title and body and title not in body:
        return f"{title} - {body}"
    return title or body or fallback


def experiment_metric_text(metadata: dict[str, Any]) -> str:
    value = metadata.get("metric_value")
    if value in (None, ""):
        return ""
    name = metadata.get("metric_name") or "metric"
    unit = metadata.get("metric_unit") or ""
    direction = metadata.get("result_direction") or metadata.get("decision") or ""
    return " ".join(part for part in [f"{name}={value}{unit}", str(direction)] if part)


def event_clock(event: dict[str, Any]) -> str:
    compact = _compact_time(str(event.get("created_at") or ""))
    if len(compact) >= 16 and compact[10:11] == " ":
        return compact[11:16]
    return "" if compact == "?" else _one_line(compact, 5)


def _event_hour(event: dict[str, Any]) -> str:
    compact = _compact_time(str(event.get("created_at") or ""))
    if len(compact) >= 13 and compact[10:11] == " ":
        return f"{compact[:13]}:00"
    if len(compact) >= 2:
        return compact
    return "recent"


def friendly_error_text(text: str) -> str:
    lowered = text.lower()
    if "key limit exceeded" in lowered:
        return "Provider key limit exceeded. Update the key limit or switch models."
    if "authenticationerror" in lowered or "user not found" in lowered or "401" in lowered:
        return "Model authentication failed. Update the API key with /api-key, then try again."
    if "permissiondeniederror" in lowered or "403" in lowered:
        return "Provider permission denied. Check model access or key limits."
    return _one_line(clean_step_summary(text), 220)


def brief_reflection_text(text: str) -> str:
    clean = clean_step_summary(text)
    match = re.search(r"Reflection through step #?([0-9]+):\s*(.*?)(?:\. Best |\.\s*$|$)", clean)
    if match:
        counts = match.group(2)
        counts = counts.replace(", 0 active operator messages", "")
        counts = counts.replace(", 0 recent finding artifacts", "")
        return _one_line(f"reflected #{match.group(1)}: {counts}", 140)
    return _one_line(clean, 140)


def generic_display_text(value: Any) -> str:
    return " ".join(str(value).split())


def clean_step_summary(summary: Any) -> str:
    text = " ".join(str(summary).split())
    if text.startswith("write_artifact saved ") and " at /" in text:
        return text.split(" at /", 1)[0]
    return text


def _chat_message_paragraphs(value: Any) -> list[str]:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(?<!^)\s(?=(?:[0-9]+\.|[-*])\s+)", "\n", text)
    paragraphs: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.strip().split())
        if line:
            paragraphs.append(line)
    return paragraphs or [""]


def _chat_agent_message_text(title: str, body: str) -> str:
    lowered = title.lower()
    if lowered == "chat":
        return body
    if lowered in {"plan", "planning"}:
        plan_body = body.split("Questions:", 1)[0]
        tasks = len(re.findall(r"(?:^|\s)- ", plan_body))
        if tasks:
            return f"Plan drafted with {tasks} items. Reply with changes or start work from the controls."
        return "Plan drafted. Reply with changes or start work from the controls."
    if lowered in {"progress", "update", "report"}:
        return _one_line(clean_step_summary(body), 220)
    return ""


def _regex_group(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def _short_url(url: str) -> str:
    if not url:
        return ""
    stripped = url.replace("https://", "").replace("http://", "")
    return stripped.split("/", 1)[0] or stripped


def _short_command(command: str) -> str:
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    if parts[0] == "ssh":
        host = next((part for part in parts[1:] if not part.startswith("-") and "=" not in part), "")
        remote = " ".join(parts[parts.index(host) + 1 :]) if host in parts else ""
        if remote:
            remote_parts = remote.split()
            remote_head = remote_parts[0] if remote_parts else "remote"
            return f"ssh {host} {remote_head}"
        return f"ssh {host}".strip()
    if parts[0] in {"python", "python3", "uv", "npm", "pnpm", "yarn", "node"} and len(parts) > 1:
        return " ".join(parts[:3])
    return " ".join(parts[:2])


def _short_path(path: Path | str, *, max_width: int = 80) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        text = "~" + text[len(home) :]
    if len(text) <= max_width:
        return text
    keep = max(12, max_width - 4)
    return "..." + text[-keep:]


def _compact_time(value: str) -> str:
    text = value.replace("T", " ")
    if len(text) >= 16 and text[4:5] == "-" and text[13:14] == ":":
        return text[:16]
    return _one_line(text, 16)
