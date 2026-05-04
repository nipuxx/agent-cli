"""Daemon process control helpers used by CLI commands."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from nipux_cli.config import AppConfig, load_config
from nipux_cli.cli_state import clear_model_setup_verified, mark_model_setup_verified
from nipux_cli.daemon import daemon_lock_status
from nipux_cli.doctor import run_doctor


ReadyFn = Callable[[Any], bool]
StartFn = Callable[[argparse.Namespace], None]
StopFn = Callable[[Any], bool]
PidAliveFn = Callable[[int], bool]


def remote_model_preflight_failures(config: Any, *, doctor_fn: Callable[..., list[Any]] = run_doctor) -> list[str]:
    blocking = {"model_config", "model_auth", "model_endpoint", "model_generation"}
    checks = doctor_fn(config=config, check_model=True)
    return [f"{check.name}: {check.detail}" for check in checks if not check.ok and check.name in blocking]


def ensure_remote_model_ready_for_worker(
    config: Any,
    *,
    fake: bool,
    doctor_fn: Callable[..., list[Any]] = run_doctor,
) -> bool:
    if fake:
        return True
    failures = remote_model_preflight_failures(config, doctor_fn=doctor_fn)
    if not failures:
        mark_model_setup_verified(config)
        return True
    clear_model_setup_verified()
    print("model is not ready; daemon not started")
    for failure in failures:
        print(f"  fail {failure}")
    print("Run `nipux doctor --check-model` after fixing the model configuration.")
    return False


def cmd_start_impl(
    args: argparse.Namespace,
    *,
    ready_fn: Callable[[Any, bool], bool],
    stop_fn: Callable[[AppConfig, float, bool], bool],
) -> None:
    config = load_config()
    config.ensure_dirs()
    status = daemon_lock_status(config.runtime.home / "agentd.lock")
    if status["running"]:
        metadata = status.get("metadata") or {}
        if status.get("stale"):
            print(f"nipux daemon stale pid={metadata.get('pid', 'unknown')}; restarting")
            stop_fn(config, 5.0, True)
            time.sleep(0.5)
        else:
            print(f"nipux daemon already running pid={metadata.get('pid', 'unknown')}")
            return
    if not ready_fn(config, bool(args.fake)):
        return
    log_path = Path(args.log_file).expanduser() if args.log_file else config.runtime.logs_dir / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "nipux_cli.cli",
        "daemon",
        "--poll-seconds",
        str(args.poll_seconds),
    ]
    if args.fake:
        command.append("--fake")
    command.append("--quiet" if args.quiet else "--verbose")
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(0.5)
    status = daemon_lock_status(config.runtime.home / "agentd.lock")
    if status["running"]:
        metadata = status.get("metadata") or {}
        print(f"nipux daemon started pid={metadata.get('pid') or process.pid}")
        print(f"log: {log_path}")
        return
    if process.poll() is None:
        print(f"nipux daemon process started pid={process.pid}, waiting for lock")
        print(f"log: {log_path}")
        return
    raise SystemExit(f"nipux daemon exited immediately with code {process.returncode}; see {log_path}")


def start_daemon_if_needed_impl(
    *,
    poll_seconds: float,
    fake: bool,
    quiet: bool,
    log_file: str | None,
    start_fn: StartFn,
    stop_fn: Callable[[AppConfig, float, bool], bool],
) -> None:
    config = load_config()
    config.ensure_dirs()
    status = daemon_lock_status(config.runtime.home / "agentd.lock")
    if status["running"]:
        metadata = status.get("metadata") or {}
        if status.get("stale"):
            print(f"daemon stale pid={metadata.get('pid', 'unknown')}; restarting")
            stop_fn(config, 5.0, True)
            time.sleep(0.5)
            start_fn(argparse.Namespace(poll_seconds=poll_seconds, fake=fake, quiet=quiet, log_file=log_file))
            return
        print(f"daemon already running pid={metadata.get('pid', 'unknown')}")
        return
    start_fn(argparse.Namespace(poll_seconds=poll_seconds, fake=fake, quiet=quiet, log_file=log_file))


def cmd_restart_impl(
    args: argparse.Namespace,
    *,
    start_fn: StartFn,
    stop_fn: Callable[[AppConfig, float, bool], bool],
) -> None:
    config = load_config()
    config.ensure_dirs()
    stopped = stop_fn(config, float(args.wait), False)
    if stopped:
        time.sleep(0.5)
    start_fn(argparse.Namespace(poll_seconds=args.poll_seconds, fake=args.fake, quiet=args.quiet, log_file=args.log_file))


def stop_daemon_process_impl(
    config: AppConfig,
    *,
    wait: float,
    quiet: bool,
    pid_alive: PidAliveFn,
) -> bool:
    status = daemon_lock_status(config.runtime.home / "agentd.lock")
    if not status["running"]:
        if not quiet:
            print("nipux daemon is not running")
        return False
    metadata = status.get("metadata") or {}
    pid = metadata.get("pid")
    if not isinstance(pid, int):
        raise SystemExit("daemon is running but lock file has no pid; stop it from the terminal that owns it")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + wait
    while time.time() < deadline:
        if not pid_alive(pid):
            if not quiet:
                print(f"nipux daemon stopped pid={pid}")
            return True
        time.sleep(0.2)
    if not quiet:
        print(f"sent SIGTERM to nipux daemon pid={pid}; it may still be shutting down")
    return False
