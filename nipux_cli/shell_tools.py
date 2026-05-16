"""Shell and workspace file tools for Nipux workers."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SHELL_TIMEOUT_SECONDS = 300.0
MAX_SHELL_TIMEOUT_SECONDS = 900.0


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
    if not _command_has_executable_text(command):
        return _json({"success": False, "error": "command must include an executable or shell keyword"})
    cwd_raw = str(args.get("cwd") or "").strip()
    cwd = cwd_raw or None
    if cwd and not Path(cwd).expanduser().exists():
        return _json({"success": False, "error": f"cwd does not exist: {cwd}"})
    timeout_raw = args.get("timeout_seconds")
    timeout = float(timeout_raw) if isinstance(timeout_raw, (int, float)) else DEFAULT_SHELL_TIMEOUT_SECONDS
    timeout = _effective_timeout_seconds(command, timeout)
    max_chars_raw = args.get("max_output_chars")
    max_chars = int(max_chars_raw) if isinstance(max_chars_raw, (int, float)) else 12000
    max_chars = max(1000, min(max_chars, 50000))
    shell = "/bin/zsh" if Path("/bin/zsh").exists() else None
    env = dict(os.environ)
    env["NIPUX_JOB_ID"] = ctx.job_id
    if ctx.run_id:
        env["NIPUX_RUN_ID"] = ctx.run_id
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
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
        _register_shell_process(ctx, process, command=command, cwd=cwd or os.getcwd(), timeout_seconds=timeout)
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        assert process is not None
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
    except BaseException:
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
        raise
    finally:
        if process is not None:
            _unregister_shell_process(ctx, process.pid)
    error = _shell_error(process.returncode, stdout, stderr, command=command)
    return _json({
        "success": process.returncode == 0 and not error,
        "error": error,
        "command": command,
        "cwd": cwd or os.getcwd(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "returncode": process.returncode,
        "stdout": _truncate_output(stdout, max_chars),
        "stderr": _truncate_output(stderr, max_chars),
    })


def _effective_timeout_seconds(command: str, requested_timeout: float) -> float:
    timeout = max(1.0, min(float(requested_timeout), MAX_SHELL_TIMEOUT_SECONDS))
    command_timeout = _shell_timeout_command_seconds(command)
    if command_timeout is None:
        return timeout
    buffer = max(5.0, min(30.0, command_timeout * 0.1))
    return max(timeout, min(command_timeout + buffer, MAX_SHELL_TIMEOUT_SECONDS))


def _command_has_executable_text(command: str) -> bool:
    for char in str(command or ""):
        if char.isalnum() or char in {".", "/", "~", "_", "-"}:
            return True
    return False


def _shell_timeout_command_seconds(command: str) -> float | None:
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if Path(token).name != "timeout":
            continue
        duration = _timeout_duration_after_options(tokens[index + 1 :])
        if duration is not None:
            return duration
    return None


def _timeout_duration_after_options(tokens: list[str]) -> float | None:
    index = 0
    options_with_values = {"-k", "--kill-after", "-s", "--signal"}
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in options_with_values:
            index += 2
            continue
        if token.startswith("--kill-after=") or token.startswith("--signal="):
            index += 1
            continue
        if token.startswith("-") and token not in {"-", "-0"}:
            index += 1
            continue
        break
    if index >= len(tokens):
        return None
    return _parse_timeout_duration(tokens[index])


def _parse_timeout_duration(raw: str) -> float | None:
    match = re.fullmatch(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhd]?)", str(raw or "").strip(), flags=re.IGNORECASE)
    if not match:
        return None
    value = float(match.group("value"))
    unit = match.group("unit").lower()
    multiplier = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[unit]
    return value * multiplier


def cleanup_registered_shell_processes(home: str | Path) -> list[dict[str, Any]]:
    path = _shell_process_registry_path(home)
    records = _read_shell_process_registry(path)
    if not records:
        return []
    cleaned: list[dict[str, Any]] = []
    survivors: list[dict[str, Any]] = []
    for record in records:
        pid = _as_int(record.get("pid"))
        if pid <= 0:
            continue
        if not _pid_exists(pid):
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            survivors.append(record)
            continue
        time.sleep(0.05)
        if _pid_exists(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        record = dict(record)
        record["cleaned_at"] = datetime.now(timezone.utc).isoformat()
        cleaned.append(record)
    _write_shell_process_registry(path, survivors)
    return cleaned


def _shell_error(returncode: int | None, stdout: str, stderr: str, *, command: str = "") -> str:
    if returncode == 0:
        return _shell_success_anomaly(stdout, stderr, command=command)
    combined = "\n".join(part.strip() for part in (stderr, stdout) if part and part.strip())
    lowered = combined.lower()
    if "sudo:" in lowered and ("password" in lowered or "terminal is required" in lowered):
        return "command requires interactive sudo/password; configure non-interactive privileges or choose a non-sudo path"
    if "permission denied" in lowered:
        return "command failed with permission denied"
    missing_probe = _missing_executable_probe(command, combined)
    if missing_probe:
        return f"command probe found no executable: {missing_probe}"
    excerpt = " ".join(combined.split())[:500] if combined else "no output"
    return f"command exited with status {returncode}: {excerpt}"


def _shell_success_anomaly(stdout: str, stderr: str, *, command: str = "") -> str:
    combined = "\n".join(part.strip() for part in (stderr, stdout) if part and part.strip())
    if not combined:
        empty_probe = _empty_observation_probe(command)
        if empty_probe:
            return f"command probe produced no output despite exit status 0: {empty_probe}"
        return ""
    lowered = combined.lower()
    auth_markers = (
        "401 unauthorized",
        "403 forbidden",
        "authentication failed",
        "username/password authentication failed",
        "invalid username or password",
        "permission denied",
    )
    if _shell_sudo_password_anomaly(lowered):
        return "command output indicates interactive sudo/password requirement despite exit status 0"
    if any(marker in lowered for marker in auth_markers):
        excerpt = " ".join(combined.split())[:500]
        return f"command output indicates authentication or authorization failure despite exit status 0: {excerpt}"
    missing_probe = _missing_executable_probe(command, combined)
    if missing_probe:
        return f"command probe found no executable despite exit status 0: {missing_probe}"
    command_missing_match = _shell_missing_command_anomaly(combined)
    if command_missing_match:
        excerpt = " ".join(combined.split())[:500]
        return f"command output indicates missing command despite exit status 0: {excerpt}"
    build_error_match = _shell_build_error_anomaly(combined)
    if build_error_match:
        excerpt = " ".join(combined.split())[:500]
        return f"command output indicates build/tool failure despite exit status 0: {excerpt}"
    http_error_match = _shell_http_error_anomaly(lowered)
    if http_error_match:
        excerpt = " ".join(combined.split())[:500]
        return f"command output indicates HTTP failure despite exit status 0: {excerpt}"
    return ""


def _missing_executable_probe(command: str, combined_output: str) -> str:
    text = str(command or "").strip()
    match = re.match(r"^(?:which|command\s+-v)\s+([A-Za-z0-9_.+-]+)(?:\s|$)", text)
    if not match:
        return ""
    if not combined_output.strip() or "not found" in combined_output.lower():
        return match.group(1)
    return ""


def _empty_observation_probe(command: str) -> str:
    text = str(command or "").strip()
    if re.match(r"^(?:which|command\s+-v)\s+([A-Za-z0-9_.+-]+)(?:\s|$)", text):
        return "probe found no executable: executable lookup returned no path"
    if re.match(r"^(?:find|ls|stat|file)\b", text):
        return "read-only filesystem probe returned no observation"
    return ""


def _shell_missing_command_anomaly(text: str) -> bool:
    return bool(
        re.search(
            r"(?im)(?:^|\n)(?:/bin/sh:\s*\d+:\s*)?(?:(?:/|~)[^\s:'\"]+|[A-Za-z0-9_.+-]+):\s*(?:command not found|not found)\s*$",
            text,
        )
    )


def _shell_sudo_password_anomaly(text: str) -> bool:
    return "sudo:" in text and ("password" in text or "terminal is required" in text)


def _shell_build_error_anomaly(text: str) -> bool:
    lowered = text.lower()
    if "no rule to make target" in lowered:
        return True
    if "***" in text and "stop." in lowered:
        return True
    return bool(re.search(r"(?im)^\s*(?:make(?:\[\d+\])?:\s*)?\*\*\* .*\bstop\.\s*$", text))


def _shell_http_error_anomaly(text: str) -> bool:
    return any(f" {code} " in f" {text} " for code in ("400", "401", "403", "404", "429", "500", "502", "503", "504")) and any(
        marker in text for marker in ("http", "error", "unauthorized", "forbidden", "not found", "too many requests")
    )


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


def _register_shell_process(ctx: Any, process: subprocess.Popen[str], *, command: str, cwd: str, timeout_seconds: float) -> None:
    path = _shell_process_registry_path(ctx.config.runtime.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "pid": process.pid,
        "pgid": process.pid,
        "job_id": getattr(ctx, "job_id", ""),
        "run_id": getattr(ctx, "run_id", "") or "",
        "step_id": getattr(ctx, "step_id", "") or "",
        "command": command[:1000],
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _unregister_shell_process(ctx: Any, pid: int) -> None:
    path = _shell_process_registry_path(ctx.config.runtime.home)
    records = [record for record in _read_shell_process_registry(path) if _as_int(record.get("pid")) != pid]
    _write_shell_process_registry(path, records)


def _shell_process_registry_path(home: str | Path) -> Path:
    return Path(home).expanduser() / "runtime" / "shell_processes.jsonl"


def _read_shell_process_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _write_shell_process_registry(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _truncate_output(value: Any, max_chars: int) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n... truncated {omitted} chars ..."


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
