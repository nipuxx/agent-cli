#!/usr/bin/env python3
"""Run a deterministic local Nipux smoke test without touching ~/.nipux."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(tempfile.gettempdir()) / "nipux-local-smoke"
SMOKE_OBJECTIVE = "Local smoke: write one artifact and report status"
SMOKE_TITLE = "local smoke"


def _safe_reset_path(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
        tmp_root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return resolved == tmp_root or tmp_root in resolved.parents


def _command_text(args: list[str], home: Path) -> str:
    rendered = " ".join(shlex.quote(part) for part in args)
    return f"NIPUX_HOME={shlex.quote(str(home))} NIPUX_PLAIN=1 python -m nipux_cli {rendered}"


def _run_cli(home: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["NIPUX_HOME"] = str(home)
    env["NIPUX_PLAIN"] = "1"
    env.setdefault("PYTHONUNBUFFERED", "1")
    command = [sys.executable, "-m", "nipux_cli", *args]
    print(f"\n$ {_command_text(args, home)}")
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout.rstrip()
    if output:
        print(output)
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result


def run_smoke(home: Path, *, reset: bool, require_model: bool) -> int:
    home = home.expanduser()
    if reset and home.exists():
        if not _safe_reset_path(home):
            raise SystemExit(f"Refusing to reset non-temporary NIPUX_HOME: {home}")
        shutil.rmtree(home)

    print("Nipux local smoke test")
    print(f"repo: {REPO_ROOT}")
    print(f"home: {home}")
    print("mode: no real model calls; fake worker step only")

    _run_cli(home, ["init", "--force"])

    doctor = _run_cli(home, ["doctor"], check=False)
    if doctor.returncode != 0:
        if require_model:
            return doctor.returncode
        print("\nmodel check is not required for this smoke test; continuing without an API key")

    _run_cli(home, ["create", SMOKE_OBJECTIVE, "--title", SMOKE_TITLE, "--kind", "generic"])
    _run_cli(home, ["jobs"])
    _run_cli(home, ["work", SMOKE_TITLE, "--steps", "1", "--fake"])
    _run_cli(home, ["status", SMOKE_TITLE])
    _run_cli(home, ["artifacts", SMOKE_TITLE, "--paths"])
    _run_cli(home, ["outcomes", SMOKE_TITLE])

    print("\nSmoke test passed.")
    print("\nTry the terminal UI with the same isolated profile:")
    print(f"  NIPUX_HOME={shlex.quote(str(home))} uv run nipux")
    print("\nWhen that looks right, test your real profile:")
    print("  uv run nipux init")
    print("  uv run nipux")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a no-network Nipux smoke test in a temporary profile.")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help=f"temporary NIPUX_HOME (default: {DEFAULT_HOME})")
    parser.add_argument("--keep", action="store_true", help="reuse the existing temporary profile instead of resetting it")
    parser.add_argument("--require-model", action="store_true", help="fail if doctor cannot reach the configured model")
    args = parser.parse_args(argv)
    return run_smoke(args.home, reset=not args.keep, require_model=args.require_model)


if __name__ == "__main__":
    raise SystemExit(main())
