"""Readable event rendering shared by CLI history and chat context."""

from __future__ import annotations

import shlex
from typing import Any

from nipux_cli.metric_format import format_metric_value
from nipux_cli.tui_event_format import clean_step_summary, generic_display_text
from nipux_cli.tui_style import _one_line


def event_line(event: dict[str, Any], *, chars: int, full: bool = False) -> str:
    when, label, detail, access = event_display_parts(event, chars=chars, full=full)
    suffix = f" | {access}" if access and full else ""
    return f"{when:<16} {label:<8} {_one_line(detail + suffix, chars)}"


def event_display_parts(event: dict[str, Any], *, chars: int, full: bool = False) -> tuple[str, str, str, str]:
    when = compact_time(str(event.get("created_at") or "?"))
    kind = str(event.get("event_type") or "event")
    title = str(event.get("title") or "").strip()
    body = generic_display_text(event.get("body") or "")
    ref_table = str(event.get("ref_table") or "")
    ref_id = str(event.get("ref_id") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    label = event_label(kind, metadata)
    access = ""
    if kind == "tool_result" and metadata.get("status"):
        label = event_label(f"{kind}:{metadata.get('status')}", metadata)
    if kind == "error":
        label = "ERROR"
    if kind.startswith("tool_result") or kind == "error":
        body = clean_step_summary(body)
    if kind == "artifact":
        title = title or ref_id
        if body.startswith("/") or "/.nipux/jobs/" in body or "/jobs/job_" in body:
            body = generic_display_text(metadata.get("summary") or "saved output")
        if title:
            access = f"open: /artifact {shlex.quote(title)}"
    if kind == "operator_message" and metadata.get("mode"):
        title = f"{title or 'operator'} {metadata.get('mode')}"
    if kind == "operator_context":
        body = body or f"{metadata.get('count') or 0} message(s)"
    if kind in {"tool_call", "tool_result", "error"} and metadata.get("step_no"):
        title = f"#{metadata.get('step_no')} {title}".strip()
    if not body and kind == "artifact" and metadata.get("path"):
        body = str(metadata.get("type") or "saved artifact")
    if not body and kind == "finding" and metadata.get("category"):
        body = str(metadata.get("category") or "")
    if not body and kind == "task" and metadata.get("status"):
        body = str(metadata.get("status") or "")
    if not body and kind == "roadmap" and metadata.get("status"):
        body = str(metadata.get("status") or "")
    if not body and kind == "milestone_validation" and metadata.get("validation_status"):
        body = str(metadata.get("validation_status") or "")
    if not body and kind == "experiment":
        metric_value = metadata.get("metric_value")
        if metric_value is not None:
            body = format_metric_value(
                metadata.get("metric_name") or "metric",
                metric_value,
                metadata.get("metric_unit") or "",
            )
    if kind == "compaction":
        body = _one_line(body, min(chars, 140))
    if kind == "daemon" and title == "run started":
        body = body or str(metadata.get("model") or "")
    detail = title if title else kind
    if body:
        detail = f"{detail} - {body}"
    if ref_table and ref_id and full:
        detail = f"{detail} [{ref_table}:{ref_id}]"
    return when, label, detail, access


def event_label(kind: str, metadata: dict[str, Any]) -> str:
    if kind == "operator_message":
        mode = str(metadata.get("mode") or "")
        return "FOLLOW" if mode == "follow_up" else "USER"
    if kind == "operator_context":
        return "ACK"
    if kind == "agent_message":
        return "AGENT"
    if kind == "roadmap":
        return "ROAD"
    if kind == "milestone_validation":
        return "VALID"
    if kind == "tool_call":
        return "TOOL"
    if kind.startswith("tool_result"):
        status = str(metadata.get("status") or "")
        if status == "blocked":
            return "BLOCK"
        if status == "failed" or kind.endswith(":failed"):
            return "ERROR"
        return "DONE"
    labels = {
        "artifact": "OUTPUT",
        "compaction": "MEMORY",
        "daemon": "SYSTEM",
        "digest": "DIGEST",
        "error": "ERROR",
        "experiment": "TEST",
        "finding": "FIND",
        "lesson": "LEARN",
        "reflection": "PLAN",
        "source": "SOURCE",
        "task": "TASK",
    }
    return labels.get(kind, kind.upper()[:8])


def compact_time(value: str) -> str:
    text = value.replace("T", " ")
    if len(text) >= 16 and text[4:5] == "-" and text[13:14] == ":":
        return text[:16]
    return _one_line(text, 16)
