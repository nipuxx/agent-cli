"""Self-update helpers for source checkouts."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


GitRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def find_checkout_root(start: str | Path | None = None) -> Path | None:
    """Return the nearest enclosing git checkout for the Nipux install."""

    current = Path(start).expanduser().resolve() if start else Path(__file__).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def update_checkout(
    *,
    path: str | Path | None = None,
    allow_dirty: bool = False,
    runner: GitRunner | None = None,
) -> tuple[int, list[str]]:
    """Fast-forward the current Nipux git checkout and return output lines."""

    root = Path(path).expanduser().resolve() if path else find_checkout_root()
    if not root or not (root / ".git").exists():
        return (
            1,
            [
                "Cannot update: this Nipux install is not a git checkout.",
                "Run from a cloned repository, or update with your package manager.",
            ],
        )
    run = runner or _run_git
    top_level = run(["git", "rev-parse", "--show-toplevel"], root)
    if top_level.returncode != 0:
        return top_level.returncode, ["Cannot update: git could not identify the checkout.", *_process_lines(top_level)]
    checkout = Path(top_level.stdout.strip() or root).expanduser().resolve()
    before = _git_text(run(["git", "rev-parse", "--short", "HEAD"], checkout), fallback="unknown")
    branch = _git_text(run(["git", "branch", "--show-current"], checkout), fallback="detached")
    dirty = run(["git", "status", "--porcelain"], checkout)
    if dirty.returncode != 0:
        return dirty.returncode, ["Cannot update: git status failed.", *_process_lines(dirty)]
    if dirty.stdout.strip() and not allow_dirty:
        return (
            1,
            [
                f"Cannot update: local changes exist in {_short_path(checkout)}.",
                "Commit or stash them first, then run `nipux update` again.",
            ],
        )
    lines = [f"Updating Nipux in {_short_path(checkout)}", f"Current: {branch} @ {before}"]
    pulled = run(["git", "pull", "--ff-only"], checkout)
    lines.extend(_process_lines(pulled))
    if pulled.returncode != 0:
        return pulled.returncode, ["Update failed.", *lines]
    after = _git_text(run(["git", "rev-parse", "--short", "HEAD"], checkout), fallback=before)
    if after == before:
        lines.append("Nipux is already up to date.")
    else:
        lines.append(f"Updated Nipux: {before} -> {after}.")
    lines.append("If the daemon is running, run `nipux restart` so it loads the new code.")
    return 0, lines


def _run_git(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _process_lines(process: subprocess.CompletedProcess[str]) -> list[str]:
    output = process.stdout if isinstance(process.stdout, str) else ""
    return [line.rstrip() for line in output.splitlines() if line.strip()]


def _git_text(process: subprocess.CompletedProcess[str], *, fallback: str) -> str:
    if process.returncode != 0:
        return fallback
    value = process.stdout.strip() if isinstance(process.stdout, str) else ""
    return value or fallback


def _short_path(path: Path | str, *, max_width: int = 96) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        text = "~" + text[len(home) :]
    if len(text) <= max_width:
        return text
    return "..." + text[-max(12, max_width - 4) :]
