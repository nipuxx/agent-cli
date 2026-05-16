"""Self-update helpers for source checkouts and installed tools."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


GitRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]
CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

DEFAULT_UPDATE_REPO = "https://github.com/nipuxx/agent-cli.git"
DEFAULT_UPDATE_REF = "main"
BUILD_METADATA_DIRS = ("build", "dist")


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
    command_runner: CommandRunner | None = None,
) -> tuple[int, list[str]]:
    """Update the current Nipux install and return output lines.

    Source checkouts are fast-forwarded with git. Installed tools are refreshed
    from the configured source repository so `nipux update` works from anywhere.
    """

    root = Path(path).expanduser().resolve() if path else find_checkout_root()
    if not root or not (root / ".git").exists():
        prefix = []
        if path is not None:
            prefix.append(f"{_short_path(root)} is not a source checkout; updating the installed Nipux tool instead.")
        code, lines = _update_uv_tool_install(runner=command_runner)
        return code, [*prefix, *lines]
    run = runner or _run_git
    top_level = run(["git", "rev-parse", "--show-toplevel"], root)
    if top_level.returncode != 0:
        return top_level.returncode, ["Cannot update: git could not identify the checkout.", *_process_lines(top_level)]
    checkout = Path(top_level.stdout.strip() or root).expanduser().resolve()
    before = _git_text(run(["git", "rev-parse", "--short", "HEAD"], checkout), fallback="unknown")
    branch = _git_text(run(["git", "branch", "--show-current"], checkout), fallback="detached")
    dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], checkout)
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
    lines.extend(clean_build_metadata(checkout))
    lines.append("Update complete.")
    return 0, lines


def clean_build_metadata(root: Path) -> list[str]:
    """Remove ignored Python build metadata that can stale local checkout installs."""

    removed: list[str] = []
    for path in [*(root / name for name in BUILD_METADATA_DIRS), *root.glob("*.egg-info")]:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(_short_path(path))
    if not removed:
        return []
    joined = ", ".join(removed)
    return [f"Removed stale build metadata: {joined}"]


def _update_uv_tool_install(*, runner: CommandRunner | None = None) -> tuple[int, list[str]]:
    uv = shutil.which("uv")
    if not uv:
        return (
            1,
            [
                "Cannot update automatically because `uv` was not found.",
                "Install uv, then run `nipux update` again.",
            ],
        )
    run = runner or _run_command
    spec = _uv_tool_update_spec()
    lines = [
        "Updating installed Nipux command.",
        f"Source: {spec}",
    ]
    current = shutil.which("nipux")
    if current:
        lines.append(f"Command: {current}")
    updated = run([uv, "tool", "install", "--force", "--upgrade", "--reinstall", "--refresh", spec])
    lines.extend(_process_lines(updated))
    if updated.returncode != 0:
        return updated.returncode, ["Update failed.", *lines]
    lines.append("Nipux command refreshed from source.")
    verified = _verify_updated_command(runner=run)
    if verified:
        lines.append(verified)
    lines.append("Update complete.")
    return 0, lines


def _verify_updated_command(*, runner: CommandRunner) -> str:
    nipux = shutil.which("nipux")
    if not nipux:
        return ""
    checked = runner([nipux, "--version"])
    version_line = " ".join(_process_lines(checked)).strip()
    if checked.returncode != 0 or not version_line:
        return ""
    return f"Verified: {version_line}"


def _uv_tool_update_spec() -> str:
    """Return the direct source uv should use for installed-tool updates."""

    explicit = os.environ.get("NIPUX_UPDATE_SPEC", "").strip()
    if explicit:
        return explicit
    repo = os.environ.get("NIPUX_REPO_URL", DEFAULT_UPDATE_REPO).strip() or DEFAULT_UPDATE_REPO
    ref = os.environ.get("NIPUX_REF", DEFAULT_UPDATE_REF).strip() or DEFAULT_UPDATE_REF
    if repo.startswith(("git+", "http://", "https://", "ssh://", "file://")):
        prefix = repo if repo.startswith("git+") else f"git+{repo}"
        return f"{prefix}@{ref}"
    return f"git+{repo}@{ref}"


def _run_git(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
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
