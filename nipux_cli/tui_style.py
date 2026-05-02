"""Small terminal styling helpers shared by the CLI frame renderers."""

from __future__ import annotations

import os
import re
import sys
from typing import Any


def _fancy_ui() -> bool:
    return (
        sys.stdout.isatty()
        and os.environ.get("NO_COLOR") is None
        and os.environ.get("NIPUX_PLAIN") is None
        and os.environ.get("TERM", "") not in {"", "dumb"}
    )


def _style(text: Any, code: str) -> str:
    value = str(text)
    if not _fancy_ui():
        return value
    return f"\033[{code}m{value}\033[0m"


def _accent(text: Any) -> str:
    return _style(text, "36")


def _muted(text: Any) -> str:
    return _style(text, "2")


def _bold(text: Any) -> str:
    return _style(text, "1")


def _one_line(value: Any, width: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _fit_ansi(text: Any, width: int) -> str:
    width = max(0, int(width))
    content = str(text)
    visible = _strip_ansi(content)
    if len(visible) > width:
        content = _one_line(visible, width)
        visible = content
    return content + " " * max(0, width - len(visible))


def _center_ansi(text: str, width: int) -> str:
    text_width = len(_strip_ansi(text))
    if text_width >= width:
        return _fit_ansi(text, width)
    left_pad = max(0, (width - text_width) // 2)
    return _fit_ansi(" " * left_pad + text, width)


def _themed_lines(lines: list[str], *, width: int) -> list[str]:
    if not _fancy_ui():
        return [_fit_ansi(line, width) for line in lines]
    bg = "\033[48;5;235m\033[38;5;252m"
    reset = "\033[0m"
    return [bg + _fit_ansi(line, width).replace(reset, reset + bg) + reset for line in lines]


def _frame_enter_sequence() -> str:
    theme = "\033[48;5;235m\033[38;5;252m" if _fancy_ui() else ""
    return f"\033[?1049h{theme}\033[2J\033[H\033[?25l\033[?1000h\033[?1002h\033[?1006h"


def _frame_exit_sequence() -> str:
    return "\033[?1006l\033[?1002l\033[?1000l\033[?25h\033[0m\033[?1049l"


def _page_indicator(active: str, pages: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for key, label in pages:
        if key == active:
            parts.append(f"{_accent('●')} {_bold(label)}")
        else:
            parts.append(f"{_muted('○')} {_muted(label)}")
    return "  ".join(parts)


def _status_badge(value: Any) -> str:
    text = str(value)
    color = {
        "active": "32",
        "advancing": "32",
        "running": "32",
        "queued": "33",
        "planning": "35",
        "waiting": "35",
        "open": "33",
        "idle": "33",
        "paused": "33",
        "cancelled": "31",
        "failed": "31",
        "completed": "36",
        "ok": "32",
        "watch": "33",
    }.get(text, "37")
    return _style(text, color)


def _event_badge(label: str) -> str:
    padded = f"{label:<8}"
    color = {
        "AGENT": "36",
        "USER": "35",
        "FOLLOW": "35",
        "YOU": "35",
        "NIPUX": "36",
        "RUN": "34",
        "TOOL": "34",
        "DONE": "32",
        "FILE": "32",
        "SAVE": "32",
        "OUTPUT": "32",
        "FIND": "32",
        "SOURCE": "36",
        "TASK": "33",
        "ROAD": "35",
        "VALID": "35",
        "TEST": "33",
        "UPDATE": "36",
        "ACK": "36",
        "FAIL": "31",
        "LEARN": "36",
        "PLAN": "36",
        "DIGEST": "36",
        "MEMORY": "36",
        "SYSTEM": "2",
        "BLOCK": "33",
        "ERROR": "31",
    }.get(label, "37")
    return _style(padded, color)
