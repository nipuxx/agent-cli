"""Shell and workspace file tools for Nipux workers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def write_file(args: dict[str, Any], ctx: Any) -> str:
    del ctx
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return _json({"success": False, "error": "path is required"})
    if "content" not in args:
        return _json({"success": False, "error": "content is required"})
    mode = str(args.get("mode") or "overwrite").strip().lower()
    if mode not in {"overwrite", "append"}:
        return _json({"success": False, "error": f"invalid mode: {mode}"})
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists() and path.is_dir():
        return _json({"success": False, "error": f"path is a directory: {path}"})
    create_parents = bool(args.get("create_parents", True))
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    content = str(args.get("content") or "")
    write_mode = "a" if mode == "append" else "w"
    with path.open(write_mode, encoding="utf-8") as fh:
        fh.write(content)
    return _json({
        "success": True,
        "path": str(path),
        "mode": mode,
        "bytes": path.stat().st_size,
    })


def shell_exec(args: dict[str, Any], ctx: Any) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        return _json({"success": False, "error": "command is required"})
    cwd_raw = str(args.get("cwd") or "").strip()
    cwd = cwd_raw or None
    if cwd and not Path(cwd).expanduser().exists():
        return _json({"success": False, "error": f"cwd does not exist: {cwd}"})
    timeout_raw = args.get("timeout_seconds")
    timeout = float(timeout_raw) if isinstance(timeout_raw, (int, float)) else 60.0
    timeout = max(1.0, min(timeout, 900.0))
    max_chars_raw = args.get("max_output_chars")
    max_chars = int(max_chars_raw) if isinstance(max_chars_raw, (int, float)) else 12000
    max_chars = max(1000, min(max_chars, 50000))
    shell = "/bin/zsh" if Path("/bin/zsh").exists() else None
    env = dict(os.environ)
    env["NIPUX_JOB_ID"] = ctx.job_id
    if ctx.run_id:
        env["NIPUX_RUN_ID"] = ctx.run_id
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            executable=shell,
            cwd=str(Path(cwd).expanduser()) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            stdout, stderr = process.communicate()
        return _json({
            "success": False,
            "error": f"command timed out after {timeout:.1f}s",
            "timed_out": True,
            "command": command,
            "cwd": cwd or os.getcwd(),
            "timeout_seconds": timeout,
            "duration_seconds": round(time.monotonic() - started, 3),
            "returncode": None,
            "stdout": _truncate_output(stdout, max_chars),
            "stderr": _truncate_output(stderr, max_chars),
        })
    error = _shell_error(process.returncode, stdout, stderr)
    return _json({
        "success": process.returncode == 0,
        "error": error,
        "command": command,
        "cwd": cwd or os.getcwd(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "returncode": process.returncode,
        "stdout": _truncate_output(stdout, max_chars),
        "stderr": _truncate_output(stderr, max_chars),
    })


def _shell_error(returncode: int | None, stdout: str, stderr: str) -> str:
    if returncode == 0:
        return ""
    combined = "\n".join(part.strip() for part in (stderr, stdout) if part and part.strip())
    lowered = combined.lower()
    if "sudo:" in lowered and ("password" in lowered or "terminal is required" in lowered):
        return "command requires interactive sudo/password; configure non-interactive privileges or choose a non-sudo path"
    if "permission denied" in lowered:
        return "command failed with permission denied"
    excerpt = " ".join(combined.split())[:500] if combined else "no output"
    return f"command exited with status {returncode}: {excerpt}"


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _truncate_output(value: Any, max_chars: int) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n... truncated {omitted} chars ..."


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
