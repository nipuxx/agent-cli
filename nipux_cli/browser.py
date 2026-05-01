"""Small `agent-browser` wrapper for the Nipux runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import hashlib
from pathlib import Path
from typing import Any

from nipux_cli.config import AppConfig
from nipux_cli.source_quality import anti_bot_reason


def _find_agent_browser() -> list[str]:
    direct = shutil.which("agent-browser")
    if direct:
        return [direct]
    if shutil.which("npx"):
        return ["npx", "--yes", "agent-browser"]
    raise FileNotFoundError("agent-browser CLI not found. Install with: npm install -g agent-browser && agent-browser install")


def _session_name(task_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)
    if len(safe) <= 32:
        return f"nipux_{safe}"
    digest = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:10]
    return f"nipux_{safe[:20]}_{digest}"


def _profile_dir(config: AppConfig, task_id: str) -> Path:
    return config.runtime.home / "browser-profiles" / _session_name(task_id)


def _socket_dir(task_id: str) -> Path:
    root = Path(os.environ.get("NIPUX_BROWSER_SOCKET_ROOT") or "/tmp")
    return root / "nipux-ab" / _session_name(task_id)


def run_browser_command(
    config: AppConfig,
    *,
    task_id: str,
    command: str,
    args: list[str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    args = args or []
    profile_dir = _profile_dir(config, task_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        *_find_agent_browser(),
        "--session",
        _session_name(task_id),
        "--session-name",
        _session_name(task_id),
        "--profile",
        str(profile_dir),
        "--json",
        command,
        *args,
    ]
    socket_dir = _socket_dir(task_id)
    socket_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "AGENT_BROWSER_SOCKET_DIR": str(socket_dir),
        "AGENT_BROWSER_SESSION_NAME": _session_name(task_id),
        "AGENT_BROWSER_PROFILE": str(profile_dir),
    }

    with tempfile.TemporaryDirectory(dir=str(socket_dir)) as tmp:
        stdout_path = Path(tmp) / "stdout"
        stderr_path = Path(tmp) / "stderr"
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return {"success": False, "error": f"browser command timed out after {timeout}s"}
        stdout_text = stdout_path.read_text(encoding="utf-8").strip()
        stderr_text = stderr_path.read_text(encoding="utf-8").strip()

    if stdout_text:
        try:
            result = json.loads(stdout_text)
        except json.JSONDecodeError:
            return {"success": False, "error": f"agent-browser returned non-JSON output: {stdout_text[:1000]}"}
        if isinstance(result, dict):
            result.setdefault("browser_session", _session_name(task_id))
            result.setdefault("browser_profile", str(profile_dir))
            return result
        return {"success": True, "data": result, "browser_session": _session_name(task_id), "browser_profile": str(profile_dir)}
    if proc.returncode != 0:
        return {
            "success": False,
            "error": stderr_text or f"agent-browser exited {proc.returncode}",
            "browser_session": _session_name(task_id),
            "browser_profile": str(profile_dir),
        }
    return {"success": True, "data": {}, "browser_session": _session_name(task_id), "browser_profile": str(profile_dir)}


def navigate(config: AppConfig, *, task_id: str, url: str) -> dict[str, Any]:
    result = run_browser_command(config, task_id=task_id, command="open", args=[url], timeout=90)
    if not result.get("success"):
        return result
    snapshot = run_browser_command(config, task_id=task_id, command="snapshot", args=["-c"], timeout=30)
    if snapshot.get("success"):
        result["snapshot"] = snapshot.get("data", {}).get("snapshot", "")
        result["refs"] = snapshot.get("data", {}).get("refs", {})
    return _annotate_source_quality(result)


def snapshot(config: AppConfig, *, task_id: str, full: bool = False) -> dict[str, Any]:
    return _annotate_source_quality(run_browser_command(config, task_id=task_id, command="snapshot", args=[] if full else ["-c"]))


def click(config: AppConfig, *, task_id: str, ref: str) -> dict[str, Any]:
    result = run_browser_command(config, task_id=task_id, command="click", args=[ref if ref.startswith("@") else f"@{ref}"])
    return _with_recovery_snapshot(config, task_id=task_id, result=result)


def fill(config: AppConfig, *, task_id: str, ref: str, text: str) -> dict[str, Any]:
    result = run_browser_command(config, task_id=task_id, command="fill", args=[ref if ref.startswith("@") else f"@{ref}", text])
    return _with_recovery_snapshot(config, task_id=task_id, result=result)


def scroll(config: AppConfig, *, task_id: str, direction: str) -> dict[str, Any]:
    return run_browser_command(config, task_id=task_id, command="scroll", args=[direction, "500"])


def back(config: AppConfig, *, task_id: str) -> dict[str, Any]:
    return run_browser_command(config, task_id=task_id, command="back")


def press(config: AppConfig, *, task_id: str, key: str) -> dict[str, Any]:
    return run_browser_command(config, task_id=task_id, command="press", args=[key])


def console(config: AppConfig, *, task_id: str, clear: bool = False, expression: str | None = None) -> dict[str, Any]:
    if expression is not None:
        return run_browser_command(config, task_id=task_id, command="eval", args=[expression])
    args = ["--clear"] if clear else []
    console_result = run_browser_command(config, task_id=task_id, command="console", args=args)
    errors_result = run_browser_command(config, task_id=task_id, command="errors", args=args)
    return {
        "success": bool(console_result.get("success") or errors_result.get("success")),
        "console": console_result,
        "errors": errors_result,
    }


def _annotate_source_quality(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    reason = anti_bot_reason(
        str(data.get("title") or ""),
        str(data.get("url") or data.get("origin") or ""),
        str(result.get("snapshot") or data.get("snapshot") or ""),
    )
    if not reason:
        return result
    result["source_warning"] = reason
    warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
    warnings.append({
        "type": "anti_bot",
        "message": reason,
        "guidance": "This page may require normal human browser verification. Do not bypass protections; continue only with visible browser actions or choose another source if stuck.",
    })
    result["warnings"] = warnings
    return result


def _with_recovery_snapshot(config: AppConfig, *, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("success", True):
        return result
    error = str(result.get("error") or "")
    if "unknown ref" not in error.lower():
        return result
    recovery = run_browser_command(config, task_id=task_id, command="snapshot", args=["-c"], timeout=30)
    result["recovery_guidance"] = "The ref was stale or missing. Use refs from recovery_snapshot before clicking or typing again."
    result["recovery_snapshot"] = _annotate_source_quality(recovery)
    return result
