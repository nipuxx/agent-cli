"""Compact event rendering helpers for the Nipux terminal UI."""

from __future__ import annotations

import re
import textwrap
from typing import Any

from nipux_cli.tui_event_format import (
    brief_reflection_text,
    chat_message_paragraphs,
    event_clock,
    event_title_body,
    friendly_error_text,
    generic_display_text,
    tool_live_summary,
)
from nipux_cli.tui_style import (
    _accent,
    _bold,
    _center_ansi,
    _fit_ansi,
    _muted,
    _one_line,
    _style,
)

THINKING_NOTICE_PREFIX = "__nipux_thinking__:"
WAITING_NOTICE_PREFIX = "__nipux_waiting__:"

LOW_VALUE_CHAT_NOTICES = (
    "sent; waiting for model",
    "sent, waiting for model",
    "waiting for model",
    "waiting for the next worker step",
)

NIPUX_HERO = [
    "███╗   ██╗██╗██████╗ ██╗   ██╗██╗  ██╗",
    "████╗  ██║██║██╔══██╗██║   ██║╚██╗██╔╝",
    "██╔██╗ ██║██║██████╔╝██║   ██║ ╚███╔╝ ",
    "██║╚██╗██║██║██╔═══╝ ██║   ██║ ██╔██╗ ",
    "██║ ╚████║██║██║     ╚██████╔╝██╔╝ ██╗",
    "╚═╝  ╚═══╝╚═╝╚═╝      ╚═════╝ ╚═╝  ╚═╝",
]

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

GENERIC_CHAT_NOTICE_PREFIXES = (
    "opened ",
    "focus set",
    "paused ",
    "resumed ",
    "cancelled ",
    "deleted ",
)


def chat_event_parts(event: dict[str, Any]) -> tuple[str, str, str] | None:
    kind = str(event.get("event_type") or "")
    title = str(event.get("title") or "").strip()
    body = str(event.get("body") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    clock = event_clock(event)
    if kind == "operator_message":
        return "YOU", body, clock
    if kind == "agent_message" and title == "chat":
        if metadata.get("error"):
            body = friendly_error_text(body)
        if _is_low_value_chat_notice(_normalized_chat_body(body)) or _is_waiting_notice(_normalized_chat_body(body)):
            return None
        return "AGENT", body, clock
    return None


def append_chat_output(lines: list[str], label: str, body: Any, *, clock: str, width: int) -> None:
    label_text = _chat_label(label)
    meta = f"{label_text} {_muted(clock)}" if clock else label_text
    lines.append(_fit_ansi(meta, width))
    prefix = "  "
    available = max(18, width - len(prefix))
    for paragraph in chat_message_paragraphs(body):
        wrapped = textwrap.wrap(paragraph, width=available) or [""]
        for part in wrapped:
            lines.append(_fit_ansi(prefix + part, width))
    if len(lines) == 1:
        lines.append(_fit_ansi(prefix, width))


def chat_pane_lines(events: list[dict[str, Any]], notices: list[str], *, width: int, rows: int) -> list[str]:
    items: list[tuple[str, str, str]] = []
    seen_chat_bodies: set[tuple[str, str]] = set()
    seen_bodies: set[str] = set()
    for event in events:
        rendered = chat_event_parts(event)
        if not rendered:
            continue
        label, body, clock = rendered
        items.append((label, body, clock))
        normalized = _normalized_chat_body(body)
        seen_chat_bodies.add((label, normalized))
        seen_bodies.add(normalized)
    for notice in notices:
        if notice.startswith(THINKING_NOTICE_PREFIX):
            body = notice.removeprefix(THINKING_NOTICE_PREFIX).strip() or "thinking"
            items.append(("THINKING", body, ""))
        elif notice.startswith(WAITING_NOTICE_PREFIX):
            body = notice.removeprefix(WAITING_NOTICE_PREFIX).strip() or "waiting"
            items.append(("WAITING", body, ""))
        elif notice.startswith("> "):
            body = notice[2:]
            normalized = _normalized_chat_body(body)
            if normalized and normalized not in seen_bodies and ("YOU", normalized) not in seen_chat_bodies:
                items.append(("YOU", body, ""))
                seen_bodies.add(normalized)
        else:
            body = notice
            normalized = _normalized_chat_body(body)
            if _is_low_value_chat_notice(normalized) or _is_waiting_notice(normalized) or _is_generic_chat_notice(normalized):
                continue
            if normalized and normalized not in seen_bodies and ("AGENT", normalized) not in seen_chat_bodies:
                items.append(("NIPUX", body, ""))
                seen_bodies.add(normalized)
    if not items:
        return chat_empty_state_lines(width=width, rows=rows)
    rendered_items = [_chat_item_lines(label, body, clock=clock, width=width) for label, body, clock in items[-max(4, rows) :]]
    output_rows = _flatten_chat_blocks(rendered_items)
    if len(output_rows) <= rows:
        return output_rows
    if rows <= 1:
        return output_rows[-rows:]
    newest = rendered_items[-1]
    if len(newest) >= rows:
        if rows <= 3:
            visible = newest[: rows - 1]
            hidden = len(newest) - len(visible)
            marker = _fit_ansi(_muted(f"... {hidden} more lines in /history."), width)
            return [*visible, marker]
        head = max(1, min(4, rows // 3))
        tail = max(1, rows - head - 1)
        hidden = max(0, len(newest) - head - tail)
        marker = _fit_ansi(_muted(f"... {hidden} middle lines hidden; /history shows all."), width)
        return [*newest[:head], marker, *newest[-tail:]]
    visible_blocks: list[list[str]] = [newest]
    used = len(newest)
    hidden_lines = 0
    for block in reversed(rendered_items[:-1]):
        if used + len(block) + 1 <= rows:
            visible_blocks.insert(0, block)
            used += len(block)
        else:
            hidden_lines += len(block)
    marker = _fit_ansi(_muted(f"... {hidden_lines} older chat lines hidden; /history shows all."), width)
    return [marker, *_flatten_chat_blocks(visible_blocks)][:rows]


def _chat_item_lines(label: str, body: Any, *, clock: str, width: int) -> list[str]:
    if label == "THINKING":
        return [_fit_ansi(f"{_style('AGENT', '36;1')}  {_accent(str(body))}", width)]
    if label == "WAITING":
        return [_fit_ansi(f"{_style('AGENT', '36;1')}  {_muted(str(body))}", width)]
    lines: list[str] = []
    append_chat_output(lines, label, body, clock=clock, width=width)
    return lines


def _flatten_chat_blocks(blocks: list[list[str]]) -> list[str]:
    rows: list[str] = []
    for block in blocks:
        if rows:
            rows.append("")
        rows.extend(block)
    return rows


def _chat_label(label: str) -> str:
    if label == "YOU":
        return _style("you", "35;1")
    if label == "AGENT":
        return _style("nipux", "36;1")
    if label == "THINKING":
        return _style("thinking", "36")
    if label == "WAITING":
        return _style("waiting", "36")
    return _muted(label.lower())


def _normalized_chat_body(value: Any) -> str:
    return " ".join(str(value or "").split())


def _is_low_value_chat_notice(normalized: str) -> bool:
    lowered = normalized.lower()
    return any(phrase in lowered for phrase in LOW_VALUE_CHAT_NOTICES)


def _is_waiting_notice(normalized: str) -> bool:
    lowered = normalized.lower()
    return (
        lowered.startswith("waiting:")
        or lowered.startswith("waiting for ")
        or "message saved for the worker" in lowered
    )


def _is_generic_chat_notice(normalized: str) -> bool:
    lowered = normalized.lower()
    return any(lowered.startswith(prefix) for prefix in GENERIC_CHAT_NOTICE_PREFIXES)


def chat_empty_state_lines(*, width: int, rows: int) -> list[str]:
    content = [
        _center_ansi(_bold(_accent("NIPUX")), width),
        "",
        *_centered_wrapped_hint("Type a goal in plain English to start a worker.", width=width),
        *_centered_wrapped_hint("Or use /new OBJECTIVE.", width=width),
        *_centered_wrapped_hint("/settings configures provider/tools.", width=width),
    ]
    top_pad = max(0, (rows - len(content)) // 2)
    return ([""] * top_pad + content)[:rows]


def _centered_wrapped_hint(text: str, *, width: int) -> list[str]:
    available = max(24, min(68, width - 4))
    return [_center_ansi(_muted(part), width) for part in textwrap.wrap(text, width=available) or [text]]


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
        rendered.append(f"{live_badge(line)} {_one_line(live_display_text(line, count=count), max(16, width - 9))}")
    return rendered


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
    if badge_text.startswith("finding"):
        return _style("find", "32")
    if badge_text.startswith("source"):
        return _style("src ", "36")
    if badge_text.startswith("experiment"):
        return _style("test", "33")
    if badge_text.startswith("task"):
        return _style("task", "33")
    if badge_text.startswith("learned"):
        return _style("mem ", "36")
    if badge_text.startswith("reflect"):
        return _style("plan", "35")
    if badge_text.startswith("update"):
        return _style("note", "35")
    return _style("info", "2")


def live_display_text(text: str, *, count: int = 1) -> str:
    if count > 1 and (
        text.startswith("error")
        or text.startswith("failed")
        or text.startswith("blocked")
    ):
        return f"x{count} {text}"
    base = text
    for prefix in (
        "start ",
        "done ",
        "saved ",
        "finding ",
        "source ",
        "experiment ",
        "task ",
        "learned ",
        "reflect ",
        "update ",
    ):
        if text.startswith(prefix):
            base = text[len(prefix) :]
            break
    if count > 1:
        return f"{base} x{count}"
    return base
