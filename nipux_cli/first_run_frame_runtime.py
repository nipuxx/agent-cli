"""Terminal runtime for the first-run Nipux workspace."""

from __future__ import annotations

import select
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import Callable

from nipux_cli.settings import inline_setting_notice
from nipux_cli.tui_commands import FIRST_RUN_SLASH_COMMANDS, autocomplete_slash
from nipux_cli.tui_input import (
    decode_terminal_escape,
    drain_pending_input,
    read_escape_sequence,
    read_terminal_char,
)


@dataclass(frozen=True)
class FirstRunRuntimeDeps:
    render_frame: Callable[[str, list[str], int, str, str | None, str], str]
    actions: Callable[[str], list[tuple[str, str, str]]]
    handle_action: Callable[[str], tuple[str, str | list[str] | None]]
    handle_line: Callable[[str], tuple[str, str | list[str] | None]]
    click_action: Callable[[int, int, str], int | None]


def run_first_run_frame(*, deps: FirstRunRuntimeDeps) -> str | None:
    buffer = ""
    notices: list[str] = []
    next_job_id: str | None = None
    view = "start"
    selected = 0
    editing_field: str | None = None
    old_attrs = termios.tcgetattr(sys.stdin)
    print("\033[?1049h\033[H\033[?25l\033[?1000h\033[?1002h\033[?1006h", end="", flush=True)
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
                last_frame = deps.render_frame(buffer, notices, selected, view, editing_field, last_frame)
                needs_render = False
                last_render = now
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
                    return None
                needs_render = True
                continue
            if char in {"\r", "\n"}:
                action, payload = _submit_first_run_line(buffer, selected=selected, view=view, deps=deps)
                buffer = ""
                state = _apply_first_run_action(action, payload, view=view, selected=selected, notices=notices)
                view, selected, editing_field, next_job_id, should_exit = state
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
                buffer = autocomplete_slash(buffer, FIRST_RUN_SLASH_COMMANDS)
                needs_render = True
                continue
            if char == "\x1b":
                view, selected, editing_field, next_job_id, should_exit = _handle_first_run_escape(
                    stdin_fd,
                    view=view,
                    selected=selected,
                    notices=notices,
                    deps=deps,
                )
                if should_exit:
                    return None
                needs_render = True
                continue
            if char.isprintable():
                buffer += char
                needs_render = True
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        print("\033[?1002l\033[?1000l\033[?1006l\033[?25h\033[0m\033[?1049l", flush=True)
    return next_job_id


def clamp_selection(selected: int, actions: list[tuple[str, str, str]]) -> int:
    if not actions:
        return 0
    return max(0, min(selected, len(actions) - 1))


def _submit_first_run_line(
    buffer: str,
    *,
    selected: int,
    view: str,
    deps: FirstRunRuntimeDeps,
) -> tuple[str, str | list[str] | None]:
    line = buffer.strip()
    if not line:
        return deps.handle_action(deps.actions(view)[selected][0])
    return deps.handle_line(line)


def _handle_first_run_escape(
    stdin_fd: int,
    *,
    view: str,
    selected: int,
    notices: list[str],
    deps: FirstRunRuntimeDeps,
) -> tuple[str, int, str | None, str | None, bool]:
    key, payload = decode_terminal_escape(read_escape_sequence("\x1b", fd=stdin_fd))
    if key == "up":
        actions = deps.actions(view)
        return view, (selected - 1) % len(actions), None, None, False
    if key == "down":
        actions = deps.actions(view)
        return view, (selected + 1) % len(actions), None, None, False
    if key in {"left", "right"}:
        return view, 0, None, None, False
    if key == "click" and isinstance(payload, tuple):
        clicked = deps.click_action(payload[0], payload[1], view)
        if clicked is not None:
            action, payload = deps.handle_action(deps.actions(view)[clicked][0])
            return _apply_first_run_action(action, payload, view=view, selected=clicked, notices=notices)
    drain_pending_input(stdin_fd)
    return view, selected, None, None, False


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
        _append_notice(notices, inline_setting_notice(editing_field, buffer), limit=10)
        return "", None, False
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


def _append_notice(notices: list[str], message: str, *, limit: int = 10) -> None:
    notices.append(message)
    notices[:] = notices[-limit:]
