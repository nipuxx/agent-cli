"""Uninstall helpers for local Nipux runtime state."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nipux_cli.config import get_agent_home
from nipux_cli.service_install import launch_agent_path, systemd_service_path


Runner = Callable[..., subprocess.CompletedProcess[str]]


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
    if systemctl:
        if dry_run:
            lines.append("would disable systemd user service nipux.service")
        else:
            runner([systemctl, "--user", "disable", "--now", "nipux.service"], check=False)
            runner([systemctl, "--user", "daemon-reload"], check=False)
            lines.append("disabled systemd user service nipux.service")
    elif systemd_service_path().exists():
        lines.append("systemd unavailable; removing service file only")

    if not launch_path.exists() and not systemd_service_path().exists():
        lines.append("no installed service files found")
    return lines


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
