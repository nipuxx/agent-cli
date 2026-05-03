"""Terminal chat-frame runtime helpers."""

from __future__ import annotations

import select
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import Callable
from typing import Any

from nipux_cli.settings import inline_setting_notice
from nipux_cli.tui_commands import CHAT_SLASH_COMMANDS, autocomplete_slash, cycle_slash
from nipux_cli.tui_input import (
    decode_terminal_escape,
    drain_pending_input,
    read_escape_sequence,
    read_terminal_char,
)
from nipux_cli.tui_outcomes import CHAT_RIGHT_PAGES
from nipux_cli.tui_style import _frame_enter_sequence, _frame_exit_sequence, _one_line, _strip_ansi


IDLE_REFRESH_SECONDS = 0.75
ACTIVE_INPUT_REFRESH_SECONDS = 2.0


@dataclass(frozen=True)
class ChatFrameDeps:
    load_snapshot: Callable[[str, int], dict[str, Any]]
    render_frame: Callable[[dict[str, Any], str, list[str], str, int, str | None, str], str]
    handle_chat_message: Callable[[str, str], tuple[bool, str]]
    capture_chat_command: Callable[[str, str], tuple[bool, str]]
    write_shell_state: Callable[[dict[str, str]], None]
    is_plain_chat_line: Callable[[str], bool]
    page_click: Callable[[int, int, str], str | None]


def compact_command_output(output: str) -> list[str]:
    lines = [" ".join(line.split()) for line in output.splitlines() if line.strip()]
    compacted: list[str] = []
    for line in lines:
        if line.startswith("\033[2J"):
            continue
        compacted.append(_one_line(line, 120))
    return compacted[-8:]


def frame_next_job_id(snapshot: dict[str, Any], current_job_id: str, *, direction: int) -> str | None:
    jobs = snapshot.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return None
    ids = [str(job.get("id")) for job in jobs if job.get("id")]
    if not ids:
        return None
    try:
        index = ids.index(str(current_job_id))
    except ValueError:
        index = 0
    return ids[(index + direction) % len(ids)]


def next_chat_right_view(current: str, direction: int) -> str:
    keys = [key for key, _label in CHAT_RIGHT_PAGES]
    try:
        index = keys.index(current)
    except ValueError:
        index = 0
    return keys[(index + direction) % len(keys)]


def frame_refresh_interval(input_buffer: str) -> float:
    return ACTIVE_INPUT_REFRESH_SECONDS if input_buffer else IDLE_REFRESH_SECONDS


def run_chat_frame(job_id: str, *, history_limit: int, deps: ChatFrameDeps) -> None:
    deps.write_shell_state({"focus_job_id": job_id})
    buffer = ""
    notices: list[str] = []
    right_view = "status"
    selected_control = 0
    editing_field: str | None = None
    snapshot = deps.load_snapshot(job_id, history_limit)
    job_id = str(snapshot["job_id"])
    old_attrs = termios.tcgetattr(sys.stdin)
    print(_frame_enter_sequence(), end="", flush=True)
    try:
        stdin_fd = sys.stdin.fileno()
        tty.setcbreak(stdin_fd)
        last_snapshot = 0.0
        needs_render = True
        last_frame = ""
        while True:
            now = time.monotonic()
            if now - last_snapshot >= frame_refresh_interval(buffer):
                try:
                    snapshot = deps.load_snapshot(job_id, history_limit)
                    job_id = str(snapshot["job_id"])
                    last_snapshot = now
                    needs_render = True
                except Exception as exc:
                    _append_notice(notices, f"frame refresh failed: {type(exc).__name__}")
            if needs_render:
                selected_control = 0
                last_frame = deps.render_frame(
                    snapshot,
                    buffer,
                    notices,
                    right_view,
                    selected_control,
                    editing_field,
                    last_frame,
                )
                needs_render = False
            readable, _, _ = select.select([stdin_fd], [], [], 0.05)
            if not readable:
                continue
            char = read_terminal_char(stdin_fd)
            if editing_field is not None:
                buffer, editing_field, should_exit = _handle_edit_input(
                    char,
                    buffer=buffer,
                    editing_field=editing_field,
                    notices=notices,
                    stdin_fd=stdin_fd,
                )
                if should_exit:
                    return
                needs_render = True
                continue
            if char in {"\r", "\n"}:
                keep_running, snapshot, job_id, notices, right_view = _handle_chat_submit(
                    buffer,
                    job_id=job_id,
                    history_limit=history_limit,
                    snapshot=snapshot,
                    notices=notices,
                    right_view=right_view,
                    deps=deps,
                )
                buffer = ""
                needs_render = True
                if not keep_running:
                    return
                continue
            if char in {"\x04"}:
                return
            if char == "\x03":
                buffer = ""
                _append_notice(notices, "cancelled input")
                needs_render = True
                continue
            if char in {"\x7f", "\b"}:
                buffer = buffer[:-1]
                needs_render = True
                continue
            if char == "\t":
                buffer = autocomplete_slash(buffer, CHAT_SLASH_COMMANDS)
                needs_render = True
                continue
            if char == "\x1b":
                snapshot, job_id, right_view, buffer = _handle_chat_escape(
                    stdin_fd,
                    snapshot=snapshot,
                    job_id=job_id,
                    history_limit=history_limit,
                    right_view=right_view,
                    buffer=buffer,
                    notices=notices,
                    deps=deps,
                )
                needs_render = True
                continue
            if char.isprintable():
                buffer += char
                needs_render = True
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        print(_frame_exit_sequence(), flush=True)


def emit_frame_if_changed(frame: str, previous_frame: str = "") -> str:
    if frame != previous_frame:
        if not previous_frame:
            print("\033[H" + frame, end="", flush=True)
        else:
            print(_diff_frame_update(frame, previous_frame), end="", flush=True)
    return frame


def _diff_frame_update(frame: str, previous_frame: str) -> str:
    current_lines = frame.splitlines()
    previous_lines = previous_frame.splitlines()
    output: list[str] = []
    max_lines = max(len(current_lines), len(previous_lines))
    for index in range(max_lines):
        current = current_lines[index] if index < len(current_lines) else ""
        previous = previous_lines[index] if index < len(previous_lines) else ""
        if current == previous:
            continue
        current_width = len(_strip_ansi(current))
        previous_width = len(_strip_ansi(previous))
        if current_width < previous_width:
            current += " " * (previous_width - current_width)
        output.append(f"\033[{index + 1};1H{current}")
    return "".join(output)


def _append_notice(notices: list[str], message: str, *, limit: int = 12) -> None:
    notices.append(message)
    notices[:] = notices[-limit:]


def _handle_edit_input(
    char: str,
    *,
    buffer: str,
    editing_field: str,
    notices: list[str],
    stdin_fd: int,
) -> tuple[str, str | None, bool]:
    if char in {"\r", "\n"}:
        _append_notice(notices, inline_setting_notice(editing_field, buffer))
        return "", None, False
    if char in {"\x04"}:
        return buffer, editing_field, True
    if char == "\x03":
        _append_notice(notices, "cancelled edit")
        return "", None, False
    if char in {"\x7f", "\b"}:
        return buffer[:-1], editing_field, False
    if char == "\x1b":
        key, _payload = decode_terminal_escape(read_escape_sequence(char, fd=stdin_fd))
        if key == "unknown":
            _append_notice(notices, "cancelled edit")
            return "", None, False
        return buffer, editing_field, False
    if char.isprintable():
        return buffer + char, editing_field, False
    return buffer, editing_field, False


def _handle_chat_submit(
    buffer: str,
    *,
    job_id: str,
    history_limit: int,
    snapshot: dict[str, Any],
    notices: list[str],
    right_view: str,
    deps: ChatFrameDeps,
) -> tuple[bool, dict[str, Any], str, list[str], str]:
    line = buffer.strip()
    if not line:
        return True, snapshot, job_id, notices, right_view
    if line in {"clear", "/clear"}:
        notices.clear()
        return True, snapshot, job_id, notices, right_view
    if line in {"settings", "/settings", "/config"}:
        _append_notice(notices, "opened settings")
        return True, snapshot, job_id, notices, "settings"
    if line in {"jobs", "/jobs", "status", "/status"}:
        _append_notice(notices, "opened jobs")
        return True, snapshot, job_id, notices, "status"
    if line in {"work", "/work", "activity", "/activity"}:
        _append_notice(notices, "opened worker")
        return True, snapshot, job_id, notices, "work"
    if line in {"outcomes", "/outcomes", "updates", "/updates"}:
        _append_notice(notices, "opened outcomes")
        return True, snapshot, job_id, notices, "updates"
    _append_notice(notices, f"> {line}")
    if deps.is_plain_chat_line(line):
        keep_running, message = deps.handle_chat_message(job_id, line)
        notices = [notice for notice in notices if notice != f"> {line}"]
        if message:
            _append_notice(notices, message)
    else:
        keep_running, output = deps.capture_chat_command(job_id, line)
        for output_line in compact_command_output(output):
            _append_notice(notices, output_line)
    snapshot = deps.load_snapshot(job_id, history_limit)
    job_id = str(snapshot["job_id"])
    return keep_running, snapshot, job_id, notices, right_view


def _handle_chat_escape(
    stdin_fd: int,
    *,
    snapshot: dict[str, Any],
    job_id: str,
    history_limit: int,
    right_view: str,
    buffer: str,
    notices: list[str],
    deps: ChatFrameDeps,
) -> tuple[dict[str, Any], str, str, str]:
    key, payload = decode_terminal_escape(read_escape_sequence("\x1b", fd=stdin_fd))
    if key in {"up", "down"} and buffer.startswith("/"):
        buffer = cycle_slash(buffer, CHAT_SLASH_COMMANDS, direction=-1 if key == "up" else 1)
        return snapshot, job_id, right_view, buffer
    if key == "right" and not buffer:
        return snapshot, job_id, next_chat_right_view(right_view, 1), buffer
    if key == "left" and not buffer:
        return snapshot, job_id, next_chat_right_view(right_view, -1), buffer
    if key in {"up", "down"} and not buffer:
        next_focus = frame_next_job_id(snapshot, job_id, direction=-1 if key == "up" else 1)
        if next_focus and next_focus != job_id:
            job_id = next_focus
            deps.write_shell_state({"focus_job_id": job_id})
            snapshot = deps.load_snapshot(job_id, history_limit)
            title = snapshot["job"].get("title") or job_id
            _append_notice(notices, f"focus {title}")
        return snapshot, job_id, right_view, buffer
    if key == "click" and isinstance(payload, tuple):
        clicked_view = deps.page_click(payload[0], payload[1], right_view)
        if clicked_view:
            return snapshot, job_id, clicked_view, buffer
    drain_pending_input(stdin_fd)
    return snapshot, job_id, right_view, buffer
