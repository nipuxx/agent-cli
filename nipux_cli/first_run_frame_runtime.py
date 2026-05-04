"""Terminal runtime for the first-run Nipux workspace."""

from __future__ import annotations

import select
import shutil
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from nipux_cli.config import load_config
from nipux_cli.settings import inline_setting_notice
from nipux_cli.tui_commands import FIRST_RUN_SLASH_COMMANDS, autocomplete_slash, cycle_slash, slash_completion_for_submit
from nipux_cli.tui_input import (
    decode_terminal_escape,
    drain_pending_input,
    read_escape_sequence,
    read_terminal_char,
)
from nipux_cli.tui_style import _frame_enter_sequence, _frame_exit_sequence


@dataclass(frozen=True)
class FirstRunRuntimeDeps:
    render_frame: Callable[[str, list[str], int, str, str | None, str], str]
    actions: Callable[[str], list[tuple[str, str, str]]]
    handle_action: Callable[[str], tuple[str, str | list[str] | None]]
    handle_line: Callable[[str], tuple[str, str | list[str] | None]]
    click_action: Callable[[int, int, str], int | str | None]


def run_first_run_frame(*, deps: FirstRunRuntimeDeps) -> str | None:
    buffer = ""
    notices: list[str] = []
    next_job_id: str | None = None
    view = "endpoint"
    selected = 0
    editing_field: str | None = required_first_run_edit_field(view)
    old_attrs = termios.tcgetattr(sys.stdin)
    print(_frame_enter_sequence(), end="", flush=True)
    try:
        stdin_fd = sys.stdin.fileno()
        tty.setcbreak(stdin_fd)
        needs_render = True
        last_render = 0.0
        last_frame = ""
        while next_job_id is None:
            now = time.monotonic()
            if needs_render or now - last_render >= 1.0:
                selected = clamp_selection(selected, deps.actions(view))
                last_frame = _safe_render_frame(
                    deps,
                    buffer=buffer,
                    notices=notices,
                    selected=selected,
                    view=view,
                    editing_field=editing_field,
                    previous_frame=last_frame,
                )
                needs_render = False
                last_render = now
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
                previous_edit = editing_field
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
                    return None
                if previous_edit and editing_field is None:
                    next_view = next_first_run_view_after_edit(view)
                    if next_view:
                        view = next_view
                        selected = 0
                        editing_field = required_first_run_edit_field(view)
                needs_render = True
                continue
            if char in {"\r", "\n"}:
                buffer, should_submit = slash_completion_for_submit(buffer, FIRST_RUN_SLASH_COMMANDS)
                if not should_submit:
                    needs_render = True
                    continue
                try:
                    action, payload = _submit_first_run_line(buffer, selected=selected, view=view, deps=deps)
                except Exception as exc:
                    action, payload = "notice", f"input failed: {type(exc).__name__}: {_one_line(exc, 100)}"
                buffer = ""
                try:
                    state = _apply_first_run_action(action, payload, view=view, selected=selected, notices=notices)
                except Exception as exc:
                    _append_notice(notices, f"action failed: {type(exc).__name__}: {_one_line(exc, 100)}")
                    state = (view, selected, None, None, False)
                view, selected, editing_field, next_job_id, should_exit = state
                editing_field = editing_field or required_first_run_edit_field(view)
                if should_exit:
                    return None
                needs_render = True
                continue
            if char in {"\x04"}:
                return None
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
                    buffer = autocomplete_slash(buffer, FIRST_RUN_SLASH_COMMANDS)
                except Exception as exc:
                    _append_notice(notices, f"autocomplete failed: {type(exc).__name__}: {_one_line(exc, 90)}")
                needs_render = True
                continue
            if char == "\x1b":
                try:
                    view, selected, editing_field, next_job_id, should_exit, buffer = _handle_first_run_escape(
                        stdin_fd,
                        view=view,
                        selected=selected,
                        buffer=buffer,
                        notices=notices,
                        deps=deps,
                    )
                except Exception as exc:
                    _append_notice(notices, f"navigation failed: {type(exc).__name__}: {_one_line(exc, 90)}")
                    should_exit = False
                if should_exit:
                    return None
                editing_field = editing_field or required_first_run_edit_field(view)
                needs_render = True
                continue
            if char.isprintable():
                buffer += char
                needs_render = True
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        print(_frame_exit_sequence(), flush=True)
    return next_job_id


def clamp_selection(selected: int, actions: list[tuple[str, str, str]]) -> int:
    if not actions:
        return 0
    return max(0, min(selected, len(actions) - 1))


def _safe_render_frame(
    deps: FirstRunRuntimeDeps,
    *,
    buffer: str,
    notices: list[str],
    selected: int,
    view: str,
    editing_field: str | None,
    previous_frame: str,
) -> str:
    try:
        return deps.render_frame(buffer, notices, selected, view, editing_field, previous_frame)
    except Exception as exc:
        _append_notice(notices, f"render failed: {type(exc).__name__}: {_one_line(exc, 100)}")
        frame = _fallback_first_run_frame(buffer=buffer, notices=notices, view=view)
        print("\033[H" + frame, end="", flush=True)
        return frame


def _fallback_first_run_frame(*, buffer: str, notices: list[str], view: str) -> str:
    width, height = shutil.get_terminal_size((100, 30))
    width = max(60, width)
    lines = [
        _fit_plain("NIPUX - setup safe mode", width),
        _fit_plain("=" * width, width),
        _fit_plain(f"Screen: {view}", width),
        _fit_plain("A UI render error was caught. You can keep typing; /exit leaves.", width),
        "",
        "Recent notices:",
    ]
    lines.extend(f"- {_one_line(notice, width - 3)}" for notice in notices[-8:])
    lines.extend(["", f"> {_one_line(buffer, width - 3)}"])
    return "\n".join(_fit_plain(line, width) for line in lines[:height])


def _submit_first_run_line(
    buffer: str,
    *,
    selected: int,
    view: str,
    deps: FirstRunRuntimeDeps,
) -> tuple[str, str | list[str] | None]:
    line = buffer.strip()
    if not line:
        actions = deps.actions(view)
        if not actions:
            return "notice", "This setup step requires an explicit value."
        return deps.handle_action(actions[clamp_selection(selected, actions)][0])
    if not line.startswith("/"):
        return "notice", "Complete the active setup field before continuing."
    return deps.handle_line(line)


def _handle_first_run_escape(
    stdin_fd: int,
    *,
    view: str,
    selected: int,
    buffer: str,
    notices: list[str],
    deps: FirstRunRuntimeDeps,
) -> tuple[str, int, str | None, str | None, bool, str]:
    key, payload = decode_terminal_escape(read_escape_sequence("\x1b", fd=stdin_fd))
    if key in {"up", "down"} and buffer.startswith("/"):
        buffer = cycle_slash(buffer, FIRST_RUN_SLASH_COMMANDS, direction=-1 if key == "up" else 1)
        return view, selected, None, None, False, buffer
    if key == "up":
        actions = deps.actions(view)
        if not actions:
            return view, selected, None, None, False, buffer
        return view, (selected - 1) % len(actions), None, None, False, buffer
    if key == "down":
        actions = deps.actions(view)
        if not actions:
            return view, selected, None, None, False, buffer
        return view, (selected + 1) % len(actions), None, None, False, buffer
    if key in {"left", "right"}:
        actions = deps.actions(view)
        if not actions:
            return view, selected, None, None, False, buffer
        delta = 1 if key == "right" else -1
        return view, (selected + delta) % len(actions), None, None, False, buffer
    if key == "click" and isinstance(payload, tuple):
        clicked = deps.click_action(payload[0], payload[1], view)
        if clicked is not None:
            if isinstance(clicked, str):
                action, payload = deps.handle_action(clicked)
                next_view, next_selected, editing_field, next_job_id, should_exit = _apply_first_run_action(
                    action,
                    payload,
                    view=view,
                    selected=selected,
                    notices=notices,
                )
                return next_view, next_selected, editing_field, next_job_id, should_exit, buffer
            actions = deps.actions(view)
            if not actions:
                return view, selected, None, None, False, buffer
            action, payload = deps.handle_action(actions[clamp_selection(clicked, actions)][0])
            next_view, next_selected, editing_field, next_job_id, should_exit = _apply_first_run_action(
                action,
                payload,
                view=view,
                selected=clicked,
                notices=notices,
            )
            return next_view, next_selected, editing_field, next_job_id, should_exit, buffer
    drain_pending_input(stdin_fd)
    return view, selected, None, None, False, buffer


def directional_first_run_action(actions: list[tuple[str, str, str]], *, direction: int) -> str | None:
    """Return the setup-screen action for left/right navigation."""

    if direction >= 0:
        for key, label, _detail in actions:
            if key.startswith("view:") and label.lower() in {"begin setup", "continue"}:
                return key
        return None
    for key, label, _detail in reversed(actions):
        if key.startswith("view:") and label.lower() == "back":
            return key
    return None


def _apply_first_run_action(
    action: str,
    payload: str | list[str] | None,
    *,
    view: str,
    selected: int,
    notices: list[str],
) -> tuple[str, int, str | None, str | None, bool]:
    if action == "view":
        notices.clear()
        return str(payload or "start"), 0, None, None, False
    if action == "exit":
        return view, selected, None, None, True
    if action == "clear":
        notices.clear()
        return view, selected, None, None, False
    if action == "open":
        return view, selected, None, str(payload), False
    if action == "edit":
        editing_field = str(payload)
        _append_notice(notices, f"editing {editing_field}; enter saves, escape cancels", limit=10)
        return view, selected, editing_field, None, False
    if isinstance(payload, list):
        for item in payload:
            if str(item).strip():
                _append_notice(notices, str(item), limit=10)
    elif payload:
        _append_notice(notices, str(payload), limit=10)
    return view, selected, None, None, False


def _handle_edit_input(
    char: str,
    *,
    buffer: str,
    editing_field: str,
    notices: list[str],
    stdin_fd: int,
) -> tuple[str, str | None, bool]:
    if char in {"\r", "\n"}:
        saved, notice = _save_first_run_edit(editing_field, buffer)
        _append_notice(notices, notice, limit=10)
        return ("", None, False) if saved else (buffer, editing_field, False)
    if char in {"\x04"}:
        return buffer, editing_field, True
    if char == "\x03":
        _append_notice(notices, "cancelled edit", limit=10)
        return "", None, False
    if char in {"\x7f", "\b"}:
        return buffer[:-1], editing_field, False
    if char == "\x1b":
        key, _payload = decode_terminal_escape(read_escape_sequence(char, fd=stdin_fd))
        if key == "unknown":
            _append_notice(notices, "cancelled edit", limit=10)
            return "", None, False
        return buffer, editing_field, False
    if char.isprintable():
        return buffer + char, editing_field, False
    return buffer, editing_field, False


def required_first_run_edit_field(view: str) -> str | None:
    return {
        "endpoint": "model.base_url",
        "api": "secret:model.api_key",
        "model": "model.name",
    }.get(view)


def next_first_run_view_after_edit(view: str) -> str | None:
    return {
        "endpoint": "api",
        "api": "model",
        "model": "access",
    }.get(view)


def _save_first_run_edit(field: str, raw_value: str) -> tuple[bool, str]:
    value = raw_value.strip()
    if field == "model.base_url":
        if not value:
            return False, "Endpoint URL is required."
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "Endpoint must be a full http:// or https:// URL."
        if not parsed.path.rstrip("/").endswith("/v1"):
            return False, "Endpoint must point at an OpenAI-compatible /v1 path."
        return True, inline_setting_notice(field, value)
    if field == "model.name":
        if not value:
            return False, "Model id is required."
        return True, inline_setting_notice(field, value)
    if field == "secret:model.api_key":
        if not value:
            return False, "API key is required, or type skip for a local endpoint."
        if value.lower() in {"skip", "none", "local"}:
            config = load_config()
            if not _is_local_endpoint(config.model.base_url):
                return False, "Only local endpoints can skip the API key."
            return True, "skipped API key for local endpoint"
        return True, inline_setting_notice(field, value)
    return True, inline_setting_notice(field, value)


def _is_local_endpoint(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local")


def _append_notice(notices: list[str], message: str, *, limit: int = 10) -> None:
    notices.append(message)
    notices[:] = notices[-limit:]


def _one_line(value: object, width: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def _fit_plain(text: object, width: int) -> str:
    content = str(text)
    if len(content) > width:
        content = _one_line(content, width)
    return content + " " * max(0, width - len(content))
