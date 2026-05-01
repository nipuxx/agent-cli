"""Terminal chat-frame runtime helpers."""

from __future__ import annotations

from typing import Any

from nipux_cli.tui_outcomes import CHAT_RIGHT_PAGES
from nipux_cli.tui_style import _one_line


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
        output.append(f"\033[{index + 1};1H\033[2K{current}")
    return "".join(output)
