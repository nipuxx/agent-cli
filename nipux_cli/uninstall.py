"""Uninstall helpers for local Nipux runtime state."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from nipux_cli.config import get_agent_home
from nipux_cli.service_install import launch_agent_path, systemd_service_path


Runner = Callable[..., subprocess.CompletedProcess[str]]
CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class UninstallPlan:
    paths: tuple[Path, ...]
    service_paths: tuple[Path, ...]


def build_uninstall_plan(*, runtime_home: Path | None = None, include_legacy: bool = True) -> UninstallPlan:
    """Return all local runtime paths that a full uninstall should remove."""

    homes = [runtime_home.expanduser() if runtime_home else get_agent_home(), get_agent_home(), Path.home() / ".nipux"]
    if include_legacy:
        homes.append(Path.home() / ".kneepucks")
    paths = tuple(_dedupe_paths(homes))
    service_paths = tuple(_dedupe_paths([launch_agent_path(), systemd_service_path()]))
    return UninstallPlan(paths=paths, service_paths=service_paths)


def uninstall_runtime(
    *,
    runtime_home: Path | None = None,
    dry_run: bool = False,
    include_legacy: bool = True,
    runner: Runner = subprocess.run,
) -> list[str]:
    """Remove local Nipux state, logs, service files, and legacy state dirs."""

    plan = build_uninstall_plan(runtime_home=runtime_home, include_legacy=include_legacy)
    lines: list[str] = []
    lines.extend(_disable_services(dry_run=dry_run, runner=runner))
    for path in (*plan.service_paths, *plan.paths):
        target = path.expanduser()
        _assert_safe_delete_target(target)
        if dry_run:
            lines.append(f"would remove {target}")
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
            lines.append(f"removed {target}")
        elif target.exists() or target.is_symlink():
            target.unlink()
            lines.append(f"removed {target}")
        else:
            lines.append(f"not found {target}")
    return lines


def uninstall_installed_tool(
    *,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
) -> tuple[int, list[str]]:
    """Remove the installed `nipux` command from common uv-tool locations."""

    uv = shutil.which("uv")
    run = runner or _run_command
    lines: list[str] = []
    if dry_run:
        lines.append("would run uv tool uninstall nipux")
        for path in installed_tool_paths():
            lines.append(f"would remove installed command path {path}")
        return 0, lines
    if uv:
        result = run([uv, "tool", "uninstall", "nipux"])
        lines.extend(_process_lines(result))
        if result.returncode == 0:
            if not lines:
                lines.append("removed installed nipux command")
            return 0, lines
        lines.append("uv tool uninstall failed; checking safe local tool paths")
    else:
        lines.append("uv not found; checking safe local tool paths")

    removed = False
    errors = 0
    for path in installed_tool_paths():
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                lines.append(f"removed {path}")
                removed = True
            elif path.exists() or path.is_symlink():
                path.unlink()
                lines.append(f"removed {path}")
                removed = True
        except OSError as exc:
            lines.append(f"failed to remove {path}: {exc}")
            errors += 1
    if removed and not errors:
        return 0, lines
    if not removed:
        lines.append("installed nipux command not found")
    return (1 if errors else 0), lines


def installed_tool_paths() -> tuple[Path, ...]:
    """Return safe user-level paths for uv-tool Nipux installs."""

    home = Path.home().expanduser().resolve(strict=False)
    candidates = [
        home / ".local" / "bin" / "nipux",
        home / ".local" / "share" / "uv" / "tools" / "nipux",
    ]
    current = shutil.which("nipux")
    if current:
        candidates.append(Path(current))
    safe: list[Path] = []
    for path in _dedupe_paths(candidates):
        expanded = path.expanduser()
        if _is_safe_installed_tool_path(expanded, home=home):
            safe.append(expanded)
    return tuple(safe)


def _disable_services(*, dry_run: bool, runner: Runner) -> list[str]:
    lines: list[str] = []
    launch_path = launch_agent_path()
    label = "gui/" + str(os.getuid()) + "/com.nipux.agent"
    launchctl = shutil.which("launchctl")
    if dry_run:
        lines.append(f"would unload launchd {label}")
    elif launchctl:
        runner([launchctl, "bootout", label], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lines.append(f"unloaded launchd {label}")
    else:
        lines.append("launchd unavailable")

    systemctl = shutil.which("systemctl")
    service_path = systemd_service_path()
    if systemctl and service_path.exists():
        if dry_run:
            lines.append("would disable systemd user service nipux.service")
        else:
            runner(
                [systemctl, "--user", "disable", "--now", "nipux.service"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            runner([systemctl, "--user", "daemon-reload"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            lines.append("disabled systemd user service nipux.service")
    elif service_path.exists():
        lines.append("systemd unavailable; removing service file only")

    if not launch_path.exists() and not service_path.exists():
        lines.append("no installed service files found")
    return lines


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
    stderr = process.stderr if isinstance(process.stderr, str) else ""
    return [line.rstrip() for line in f"{output}\n{stderr}".splitlines() if line.strip()]


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _assert_safe_delete_target(path: Path) -> None:
    resolved = path.expanduser().resolve(strict=False)
    home = Path.home().resolve(strict=False)
    forbidden = {Path("/").resolve(strict=False), home}
    if resolved in forbidden:
        raise ValueError(f"refusing to remove unsafe path: {path}")
    if len(resolved.parts) < 3:
        raise ValueError(f"refusing to remove broad path: {path}")


def _is_safe_installed_tool_path(path: Path, *, home: Path) -> bool:
    expanded = path.expanduser()
    resolved = expanded.resolve(strict=False)
    user_bin = home / ".local" / "bin" / "nipux"
    uv_tool_root = home / ".local" / "share" / "uv" / "tools" / "nipux"
    return (
        expanded == user_bin
        or resolved == user_bin
        or expanded == uv_tool_root
        or resolved == uv_tool_root
        or uv_tool_root in resolved.parents
    )
