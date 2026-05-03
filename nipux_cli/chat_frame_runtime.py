"""Terminal chat-frame runtime helpers."""

from __future__ import annotations

import select
import shutil
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
    render_frame: Callable[[dict[str, Any], str, list[str], str, int, str | None, str | None, str], str]
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
    modal_view: str | None = None
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
                last_frame = _safe_render_frame(
                    deps,
                    snapshot=snapshot,
                    buffer=buffer,
                    notices=notices,
                    right_view=right_view,
                    selected_control=selected_control,
                    editing_field=editing_field,
                    modal_view=modal_view,
                    previous_frame=last_frame,
                )
                needs_render = False
            try:
                readable, _, _ = select.select([stdin_fd], [], [], 0.05)
            except OSError as exc:
                _append_notice(notices, f"terminal read failed: {type(exc).__name__}: {_one_line(exc, 90)}")
                needs_render = True
                continue
            if not readable:
                continue
            try:
                char = read_terminal_char(stdin_fd)
            except OSError as exc:
                _append_notice(notices, f"terminal input failed: {type(exc).__name__}: {_one_line(exc, 90)}")
                needs_render = True
                continue
            if editing_field is not None:
                try:
                    buffer, editing_field, should_exit = _handle_edit_input(
                        char,
                        buffer=buffer,
                        editing_field=editing_field,
                        notices=notices,
                        stdin_fd=stdin_fd,
                    )
                except Exception as exc:
                    buffer = ""
                    editing_field = None
                    _append_notice(notices, f"edit failed: {type(exc).__name__}: {_one_line(exc, 90)}")
                    needs_render = True
                    continue
                if should_exit:
                    return
                needs_render = True
                continue
            if char in {"\r", "\n"}:
                try:
                    keep_running, snapshot, job_id, notices, right_view, modal_view = _handle_chat_submit(
                        buffer,
                        job_id=job_id,
                        history_limit=history_limit,
                        snapshot=snapshot,
                        notices=notices,
                        right_view=right_view,
                        modal_view=modal_view,
                        deps=deps,
                    )
                except Exception as exc:
                    keep_running = True
                    _append_notice(notices, f"submit failed: {type(exc).__name__}: {_one_line(exc, 100)}")
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
                try:
                    buffer = autocomplete_slash(buffer, CHAT_SLASH_COMMANDS)
                except Exception as exc:
                    _append_notice(notices, f"autocomplete failed: {type(exc).__name__}: {_one_line(exc, 90)}")
                needs_render = True
                continue
            if char == "\x1b":
                try:
                    snapshot, job_id, right_view, modal_view, buffer = _handle_chat_escape(
                        stdin_fd,
                        snapshot=snapshot,
                        job_id=job_id,
                        history_limit=history_limit,
                        right_view=right_view,
                        modal_view=modal_view,
                        buffer=buffer,
                        notices=notices,
                        deps=deps,
                    )
                except Exception as exc:
                    modal_view = None
                    _append_notice(notices, f"navigation failed: {type(exc).__name__}: {_one_line(exc, 90)}")
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


def _safe_render_frame(
    deps: ChatFrameDeps,
    *,
    snapshot: dict[str, Any],
    buffer: str,
    notices: list[str],
    right_view: str,
    selected_control: int,
    editing_field: str | None,
    modal_view: str | None,
    previous_frame: str,
) -> str:
    try:
        return deps.render_frame(
            snapshot,
            buffer,
            notices,
            right_view,
            selected_control,
            editing_field,
            modal_view,
            previous_frame,
        )
    except Exception as exc:
        _append_notice(notices, f"render failed: {type(exc).__name__}: {_one_line(exc, 100)}")
        frame = _fallback_chat_frame(snapshot=snapshot, buffer=buffer, notices=notices)
        print("\033[H" + frame, end="", flush=True)
        return frame


def _fallback_chat_frame(*, snapshot: dict[str, Any], buffer: str, notices: list[str]) -> str:
    width, height = shutil.get_terminal_size((100, 30))
    width = max(60, width)
    job = snapshot.get("job") if isinstance(snapshot.get("job"), dict) else {}
    title = str(job.get("title") or snapshot.get("job_id") or "Nipux")
    lines = [
        _fit_plain("NIPUX - safe mode", width),
        _fit_plain("=" * width, width),
        _fit_plain(f"Job: {title}", width),
        _fit_plain("A UI render error was caught. You can keep typing; /exit leaves.", width),
        "",
        "Recent notices:",
    ]
    lines.extend(f"- {_one_line(notice, width - 3)}" for notice in notices[-8:])
    lines.extend(["", f"> {_one_line(buffer, width - 3)}"])
    return "\n".join(_fit_plain(line, width) for line in lines[:height])


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


def _fit_plain(text: Any, width: int) -> str:
    content = _strip_ansi(str(text))
    if len(content) > width:
        content = _one_line(content, width)
    return content + " " * max(0, width - len(content))


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
    modal_view: str | None,
    deps: ChatFrameDeps,
) -> tuple[bool, dict[str, Any], str, list[str], str, str | None]:
    line = buffer.strip()
    if not line:
        return True, snapshot, job_id, notices, right_view, modal_view
    if line in {"clear", "/clear"}:
        notices.clear()
        return True, snapshot, job_id, notices, right_view, None
    if line in {"settings", "/settings"}:
        _append_notice(notices, "opened settings")
        return True, snapshot, job_id, notices, right_view, "settings"
    if line in {"jobs", "/jobs", "status", "/status"}:
        _append_notice(notices, "opened jobs")
        return True, snapshot, job_id, notices, "status", None
    if line in {"work", "/work", "activity", "/activity"}:
        _append_notice(notices, "opened worker")
        return True, snapshot, job_id, notices, "work", None
    if line in {"outcomes", "/outcomes", "updates", "/updates"}:
        _append_notice(notices, "opened outcomes")
        return True, snapshot, job_id, notices, "updates", None
    _append_notice(notices, f"> {line}")
    keep_running = True
    try:
        if deps.is_plain_chat_line(line):
            keep_running, message = deps.handle_chat_message(job_id, line)
            notices = [notice for notice in notices if notice != f"> {line}"]
            if message:
                _append_notice(notices, message)
            modal_view = None
        else:
            keep_running, output = deps.capture_chat_command(job_id, line)
            for output_line in compact_command_output(output):
                _append_notice(notices, output_line)
            if line.startswith(("/model", "/base-url", "/api-key", "/api-key-env", "/context", "/input-cost", "/output-cost", "/timeout", "/home", "/step-limit", "/output-chars", "/daily-digest", "/digest-time", "/config")):
                modal_view = "settings"
            else:
                modal_view = None
    except Exception as exc:
        _append_notice(notices, f"message failed: {type(exc).__name__}: {_one_line(exc, 120)}")
    try:
        snapshot = deps.load_snapshot(job_id, history_limit)
        job_id = str(snapshot["job_id"])
    except Exception as exc:
        _append_notice(notices, f"refresh failed after message: {type(exc).__name__}: {_one_line(exc, 100)}")
    return keep_running, snapshot, job_id, notices, right_view, modal_view


def _handle_chat_escape(
    stdin_fd: int,
    *,
    snapshot: dict[str, Any],
    job_id: str,
    history_limit: int,
    right_view: str,
    modal_view: str | None,
    buffer: str,
    notices: list[str],
    deps: ChatFrameDeps,
) -> tuple[dict[str, Any], str, str, str | None, str]:
    key, payload = decode_terminal_escape(read_escape_sequence("\x1b", fd=stdin_fd))
    if modal_view:
        _append_notice(notices, "closed settings")
        drain_pending_input(stdin_fd)
        return snapshot, job_id, right_view, None, buffer
    if key in {"up", "down"} and buffer.startswith("/"):
        buffer = cycle_slash(buffer, CHAT_SLASH_COMMANDS, direction=-1 if key == "up" else 1)
        return snapshot, job_id, right_view, modal_view, buffer
    if key == "right" and not buffer:
        return snapshot, job_id, next_chat_right_view(right_view, 1), modal_view, buffer
    if key == "left" and not buffer:
        return snapshot, job_id, next_chat_right_view(right_view, -1), modal_view, buffer
    if key in {"up", "down"} and not buffer:
        next_focus = frame_next_job_id(snapshot, job_id, direction=-1 if key == "up" else 1)
        if next_focus and next_focus != job_id:
            job_id = next_focus
            deps.write_shell_state({"focus_job_id": job_id})
            snapshot = deps.load_snapshot(job_id, history_limit)
            title = snapshot["job"].get("title") or job_id
            _append_notice(notices, f"focus {title}")
        return snapshot, job_id, right_view, modal_view, buffer
    if key == "click" and isinstance(payload, tuple):
        clicked_view = deps.page_click(payload[0], payload[1], right_view)
        if clicked_view:
            return snapshot, job_id, clicked_view, modal_view, buffer
    drain_pending_input(stdin_fd)
    return snapshot, job_id, right_view, modal_view, buffer
