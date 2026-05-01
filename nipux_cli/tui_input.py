"""Terminal input helpers for full-screen Nipux frames."""

from __future__ import annotations

import os
import re
import select
import sys
import time


def read_terminal_char(fd: int) -> str:
    data = os.read(fd, 1)
    return data.decode("latin1", errors="ignore")


def read_escape_sequence(first: str, *, fd: int | None = None) -> str:
    fd = sys.stdin.fileno() if fd is None else fd
    sequence = first
    deadline = time.monotonic() + 0.12
    while len(sequence) < 96:
        timeout = max(0.0, min(0.04, deadline - time.monotonic()))
        if timeout <= 0:
            break
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            break
        sequence += read_terminal_char(fd)
        if terminal_escape_complete(sequence):
            break
    return sequence


def terminal_escape_complete(sequence: str) -> bool:
    if sequence in {"\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", "\x1bOA", "\x1bOB", "\x1bOC", "\x1bOD"}:
        return True
    if re.match(r"^\x1b\[[0-9;?]*[ABCD]$", sequence):
        return True
    if re.match(r"^\x1b\[<\d+;\d+;\d+[mM]$", sequence):
        return True
    if sequence.startswith("\x1b[M") and len(sequence) >= 6:
        return True
    return False


def decode_terminal_escape(sequence: str) -> tuple[str, tuple[int, int] | None]:
    arrows = {
        "\x1b[A": "up",
        "\x1b[B": "down",
        "\x1b[C": "right",
        "\x1b[D": "left",
        "\x1bOA": "up",
        "\x1bOB": "down",
        "\x1bOC": "right",
        "\x1bOD": "left",
    }
    if sequence in arrows:
        return arrows[sequence], None
    csi_arrow = re.match(r"^\x1b\[[0-9;?]*([ABCD])$", sequence)
    if csi_arrow:
        return {"A": "up", "B": "down", "C": "right", "D": "left"}[csi_arrow.group(1)], None
    match = re.match(r"^\x1b\[<(\d+);(\d+);(\d+)([mM])$", sequence)
    if match and match.group(4) == "M":
        button = int(match.group(1))
        if button == 0:
            return "click", (int(match.group(2)), int(match.group(3)))
    if sequence.startswith("\x1b[M") and len(sequence) >= 6:
        button = ord(sequence[3]) - 32
        if button == 0:
            return "click", (ord(sequence[4]) - 32, ord(sequence[5]) - 32)
    return "unknown", None


def drain_pending_input(fd: int | None = None) -> None:
    fd = sys.stdin.fileno() if fd is None else fd
    while True:
        readable, _, _ = select.select([fd], [], [], 0)
        if not readable:
            return
        os.read(fd, 1)
