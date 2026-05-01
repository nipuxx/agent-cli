"""Shared event formatting helpers for Nipux terminal renderers."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

from nipux_cli.metric_format import format_metric_value
from nipux_cli.tui_style import _one_line


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
    return " ".join(part for part in [format_metric_value(name, value, unit), str(direction)] if part)


def event_clock(event: dict[str, Any]) -> str:
    compact = _compact_time(str(event.get("created_at") or ""))
    if len(compact) >= 16 and compact[10:11] == " ":
        return compact[11:16]
    return "" if compact == "?" else _one_line(compact, 5)


def event_hour(event: dict[str, Any]) -> str:
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


def chat_message_paragraphs(value: Any) -> list[str]:
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


def chat_agent_message_text(title: str, body: str) -> str:
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
        return f"update {short_path(path, max_width=36)}" if path else "update file"
    if tool == "defer_job":
        seconds = args.get("seconds") or args.get("delay_seconds")
        until = args.get("until")
        if until:
            return f"wait until {until}"
        return f"wait {seconds}s" if seconds else "wait before next check"
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


def short_path(path: Path | str, *, max_width: int = 80) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        text = "~" + text[len(home) :]
    if len(text) <= max_width:
        return text
    keep = max(12, max_width - 4)
    return "..." + text[-keep:]


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


def _compact_time(value: str) -> str:
    text = value.replace("T", " ")
    if len(text) >= 16 and text[4:5] == "-" and text[13:14] == ":":
        return text[:16]
    return _one_line(text, 16)
