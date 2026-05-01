"""Daemon runner for restartable background jobs."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from nipux_cli.config import AppConfig, load_config
from nipux_cli.db import AgentDB
from nipux_cli.digest import write_daily_digest
from nipux_cli.scheduling import job_deferred_until, job_is_deferred


class DaemonAlreadyRunning(RuntimeError):
    pass


RUNTIME_CODE_FILES = (
    "artifacts.py",
    "browser.py",
    "browser_web.py",
    "compression.py",
    "config.py",
    "daemon.py",
    "db.py",
    "digest.py",
    "llm.py",
    "operator_context.py",
    "progress.py",
    "source_quality.py",
    "templates.py",
    "tools.py",
    "worker.py",
)


@lru_cache(maxsize=1)
def current_runtime_fingerprint() -> dict[str, Any]:
    """Return a stable fingerprint for code that affects daemon behavior."""

    from nipux_cli import __version__
    from nipux_cli.tools import DEFAULT_REGISTRY
    from nipux_cli.worker import SYSTEM_PROMPT, WORKER_PROTOCOL_VERSION

    tool_schema = DEFAULT_REGISTRY.openai_tools()
    tool_schema_hash = hashlib.sha256(json.dumps(tool_schema, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    prompt_hash = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    code_fingerprint = _runtime_code_fingerprint()
    payload = {
        "nipux_version": __version__,
        "worker_protocol": WORKER_PROTOCOL_VERSION,
        "tool_schema_hash": tool_schema_hash[:16],
        "prompt_hash": prompt_hash[:16],
        "code_hash": code_fingerprint["code_hash"],
        "code_mtime": code_fingerprint["code_mtime"],
        "tool_count": len(DEFAULT_REGISTRY.names()),
    }
    payload["runtime_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return payload


@lru_cache(maxsize=1)
def _runtime_code_fingerprint() -> dict[str, Any]:
    package_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    mtimes: list[float] = []
    for name in RUNTIME_CODE_FILES:
        path = package_dir / name
        if not path.exists():
            continue
        digest.update(name.encode("utf-8"))
        data = path.read_bytes()
        digest.update(hashlib.sha256(data).digest())
        mtimes.append(path.stat().st_mtime)
    return {
        "code_hash": digest.hexdigest()[:16],
        "code_mtime": max(mtimes) if mtimes else 0,
    }


def runtime_stale(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    recorded = metadata.get("runtime")
    if not isinstance(recorded, dict):
        return True
    return recorded.get("runtime_hash") != current_runtime_fingerprint().get("runtime_hash")


def _parse_lock_metadata(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"raw": raw}
    except json.JSONDecodeError:
        return {"raw": raw}


def daemon_lock_status(path: str | Path) -> dict[str, Any]:
    """Return whether another process currently holds the daemon lock."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        handle.seek(0)
        metadata = _parse_lock_metadata(handle.read())
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stale = runtime_stale(metadata)
            return {
                "running": True,
                "lock_path": str(path),
                "metadata": metadata,
                "stale": stale,
                "current_runtime": current_runtime_fingerprint(),
                "detail": "daemon lock is held",
            }
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {
        "running": False,
        "lock_path": str(path),
        "metadata": metadata,
        "stale": False,
        "current_runtime": current_runtime_fingerprint(),
        "detail": "daemon lock is free",
    }


@contextlib.contextmanager
def single_instance_lock(path: str | Path):
    """Hold an exclusive non-blocking daemon lock for this state directory."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DaemonAlreadyRunning(f"Another nipux daemon holds {path}") from exc
        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "runtime": current_runtime_fingerprint(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(payload, sort_keys=True))
        handle.flush()
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def update_lock_metadata(handle, **patch: Any) -> None:
    handle.seek(0)
    metadata = _parse_lock_metadata(handle.read())
    metadata.update(patch)
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(metadata, sort_keys=True))
    handle.flush()


def append_daemon_event(config: AppConfig, event: str, **fields: Any) -> Path:
    """Append a small daemon event that the CLI can tail without parsing stdout."""

    config.ensure_dirs()
    path = config.runtime.logs_dir / "daemon-events.jsonl"
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    return path


def read_daemon_events(config: AppConfig, *, limit: int = 20) -> list[dict[str, Any]]:
    path = config.runtime.logs_dir / "daemon-events.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            events.append({"event": "unparseable", "raw": line})
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def fake_step_llm():
    from nipux_cli.llm import LLMResponse, ScriptedLLM, ToolCall

    nonce = datetime.now(timezone.utc).isoformat()
    return ScriptedLLM([
        LLMResponse(tool_calls=[
            ToolCall(
                name="write_artifact",
                arguments={
                    "title": "daemon-fake-step",
                    "type": "text",
                    "summary": "Fake daemon step",
                    "content": f"This is a fake daemon worker step.\n\nnonce: {nonce}",
                },
            )
        ])
    ])


@dataclass
class Daemon:
    config: AppConfig
    db: AgentDB

    @classmethod
    def open(cls, config: AppConfig | None = None) -> "Daemon":
        config = config or load_config()
        config.ensure_dirs()
        return cls(config=config, db=AgentDB(config.runtime.state_db_path))

    @property
    def lock_path(self) -> Path:
        return self.config.runtime.home / "agentd.lock"

    def close(self) -> None:
        self.db.close()

    def next_runnable_job(self) -> dict | None:
        """Return the next runnable job by priority/age.

        UI focus is intentionally not used here. Focus is for the operator's
        chat view; the daemon should keep all runnable jobs advancing.
        """

        now = datetime.now(timezone.utc)
        for job in self.db.list_jobs(statuses=["queued", "running"]):
            if job_is_deferred(job, now=now):
                continue
            return job
        return None

    def idle_sleep_seconds(self, *, poll_seconds: float, now: datetime | None = None) -> float:
        """Return the next idle sleep, capped by the nearest deferred job wake."""

        fallback = max(5.0, poll_seconds)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        due_times: list[datetime] = []
        for job in self.db.list_jobs(statuses=["queued", "running"]):
            due = job_deferred_until(job, now=now)
            if due is not None:
                due_times.append(due)
        if not due_times:
            return fallback
        wait_seconds = min((due - now).total_seconds() for due in due_times)
        return max(0.5, min(fallback, wait_seconds))

    def run_once(self, *, fake: bool = False, verbose: bool = False):
        from nipux_cli.worker import run_one_step

        job = self.next_runnable_job()
        if job is None:
            return None
        if verbose:
            print(f"thinking job={job['id']} title={job['title']} kind={job['kind']}", flush=True)
            print(f"objective: {job['objective']}", flush=True)
        llm = fake_step_llm() if fake else None
        return run_one_step(job["id"], config=self.config, db=self.db, llm=llm)

    def send_due_daily_digest(self, *, now: datetime | None = None) -> dict | None:
        if not self.config.runtime.daily_digest_enabled:
            return None
        now = now or datetime.now()
        if not _is_digest_due(now, self.config.runtime.daily_digest_time):
            return None
        day = now.date().isoformat()
        target = self.config.email.to_addr or "dry-run"
        if self.db.digest_exists(day=day, target=target):
            return None
        return write_daily_digest(self.config, self.db, day=day)

    def run_forever(
        self,
        *,
        fake: bool = False,
        poll_seconds: float = 30.0,
        quiet: bool = False,
        verbose: bool = False,
        max_iterations: int | None = None,
    ) -> None:
        consecutive_failures = 0
        iterations = 0
        with single_instance_lock(self.lock_path) as lock_handle:
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
            recovered = self.db.mark_interrupted_running(reason="daemon recovered abandoned running work from a previous process")
            append_daemon_event(
                self.config,
                "daemon_started",
                pid=os.getpid(),
                fake=fake,
                poll_seconds=poll_seconds,
                recovered_steps=recovered["steps"],
                recovered_runs=recovered["runs"],
                runtime=current_runtime_fingerprint(),
            )
            if recovered["steps"] or recovered["runs"]:
                append_daemon_event(self.config, "stale_work_recovered", **recovered)
            if not quiet:
                print(f"nipux daemon started; db={self.config.runtime.state_db_path}", flush=True)
            try:
                while True:
                    iterations += 1
                    update_lock_metadata(
                        lock_handle,
                        last_heartbeat=datetime.now(timezone.utc).isoformat(),
                        last_state="checking",
                        consecutive_failures=consecutive_failures,
                        runtime=current_runtime_fingerprint(),
                    )

                    try:
                        digest = self.send_due_daily_digest()
                        if digest:
                            append_daemon_event(self.config, "daily_digest", **digest)
                            if not quiet:
                                print(f"daily_digest {json.dumps(digest, ensure_ascii=False)}", flush=True)
                        result = self.run_once(fake=fake, verbose=verbose and not quiet)
                    except Exception as exc:
                        consecutive_failures += 1
                        payload = _exception_payload(exc)
                        update_lock_metadata(
                            lock_handle,
                            last_heartbeat=datetime.now(timezone.utc).isoformat(),
                            last_state="error",
                            last_error=payload["error"],
                            last_error_type=payload["error_type"],
                            consecutive_failures=consecutive_failures,
                            runtime=current_runtime_fingerprint(),
                        )
                        append_daemon_event(self.config, "daemon_error", **payload, consecutive_failures=consecutive_failures)
                        if not quiet:
                            print(
                                f"daemon_error type={payload['error_type']} error={payload['error'][:240]}",
                                flush=True,
                            )
                        _sleep_or_stop(_exception_backoff(exc, poll_seconds, consecutive_failures), max_iterations, iterations)
                        if max_iterations is not None and iterations >= max_iterations:
                            return
                        continue

                    if result is None:
                        update_lock_metadata(
                            lock_handle,
                            last_heartbeat=datetime.now(timezone.utc).isoformat(),
                            last_state="idle",
                            runtime=current_runtime_fingerprint(),
                        )
                        idle_sleep = self.idle_sleep_seconds(poll_seconds=poll_seconds)
                        if not quiet:
                            print(f"idle; sleeping {idle_sleep:g}s", flush=True)
                        _sleep_or_stop(idle_sleep, max_iterations, iterations)
                    else:
                        consecutive_failures = consecutive_failures + 1 if result.status == "failed" else 0
                        update_lock_metadata(
                            lock_handle,
                            last_heartbeat=datetime.now(timezone.utc).isoformat(),
                            last_state="step",
                            last_job_id=result.job_id,
                            last_run_id=result.run_id,
                            last_step_id=result.step_id,
                            last_status=result.status,
                            last_tool=result.tool_name,
                            last_error="" if result.status != "failed" else str(result.result.get("error") or ""),
                            last_error_type="" if result.status != "failed" else str(result.result.get("error_type") or ""),
                            consecutive_failures=consecutive_failures,
                            runtime=current_runtime_fingerprint(),
                        )
                        detail = result.result.get("error") or result.result.get("artifact_id") or result.result.get("content", "")
                        append_daemon_event(
                            self.config,
                            "step",
                            job_id=result.job_id,
                            run_id=result.run_id,
                            step_id=result.step_id,
                            status=result.status,
                            tool=result.tool_name,
                            detail=str(detail)[:500],
                            consecutive_failures=consecutive_failures,
                        )
                        if not quiet:
                            print(
                                f"step job={result.job_id} run={result.run_id} step={result.step_id} "
                                f"status={result.status} tool={result.tool_name or '-'} detail={str(detail)[:240]}",
                                flush=True,
                            )
                            if verbose:
                                print(json.dumps(result.result, ensure_ascii=False, indent=2)[:8000], flush=True)
                        sleep_seconds = (
                            _step_failure_backoff(result, poll_seconds, consecutive_failures)
                            if result.status == "failed"
                            else max(0.0, poll_seconds)
                        )
                        _sleep_or_stop(sleep_seconds, max_iterations, iterations)
                    if max_iterations is not None and iterations >= max_iterations:
                        return
            except KeyboardInterrupt:
                interrupted = self.db.mark_interrupted_running(reason="daemon stopped during active work")
                update_lock_metadata(
                    lock_handle,
                    last_heartbeat=datetime.now(timezone.utc).isoformat(),
                    last_state="stopped",
                    consecutive_failures=consecutive_failures,
                    runtime=current_runtime_fingerprint(),
                )
                append_daemon_event(self.config, "daemon_stopped", pid=os.getpid(), interrupted_steps=interrupted["steps"], interrupted_runs=interrupted["runs"])
                if not quiet:
                    print("nipux daemon stopped", flush=True)
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)


def _is_digest_due(now: datetime, configured_time: str) -> bool:
    try:
        hour_text, minute_text = configured_time.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        hour, minute = 8, 0
    return (now.hour, now.minute) >= (hour, minute)


def _raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _exception_payload(exc: Exception) -> dict[str, str]:
    return {
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _failure_backoff(poll_seconds: float, consecutive_failures: int) -> float:
    base = max(1.0, poll_seconds)
    return min(60.0, base * min(8, max(1, consecutive_failures)))


def _step_failure_backoff(result: Any, poll_seconds: float, consecutive_failures: int) -> float:
    """Return a retry delay for failed worker steps.

    Worker LLM/provider failures are recorded as failed steps rather than
    escaping as daemon exceptions, so they need the same throttling path here.
    This keeps bad credentials, quota limits, and provider throttles from
    flooding the timeline while preserving the forever loop.
    """

    fallback = _failure_backoff(poll_seconds, consecutive_failures)
    text = _step_failure_text(result)
    if _is_provider_config_text(text):
        return max(fallback, 300.0)
    if _is_rate_limit_text(text):
        return max(fallback, 60.0)
    return fallback


def _step_failure_text(result: Any) -> str:
    payload = getattr(result, "result", None)
    if not isinstance(payload, dict):
        return str(result)
    parts = [
        payload.get("error"),
        payload.get("error_type"),
        payload.get("detail"),
        payload.get("message"),
    ]
    return " ".join(str(part) for part in parts if part)


def _exception_backoff(exc: Exception, poll_seconds: float, consecutive_failures: int) -> float:
    fallback = _failure_backoff(poll_seconds, consecutive_failures)
    if not _is_rate_limit_error(exc):
        return fallback
    retry_after = _retry_after_seconds(exc)
    if retry_after is None:
        return max(fallback, 10.0)
    return max(fallback, min(300.0, retry_after))


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    return _is_rate_limit_text(f"{type(exc).__name__} {exc}")


def _is_rate_limit_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "429" in lowered
        or "rate limit" in lowered
        or "ratelimit" in lowered
        or "too many requests" in lowered
        or "temporarily over capacity" in lowered
    )


def _is_provider_config_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "401" in lowered
        or "403" in lowered
        or "authentication" in lowered
        or "permissiondenied" in lowered
        or "permission denied" in lowered
        or "invalid api key" in lowered
        or ("api key" in lowered and ("missing" in lowered or "invalid" in lowered))
        or "key limit exceeded" in lowered
        or "insufficient quota" in lowered
        or "quota exceeded" in lowered
        or "billing" in lowered
        or "credits" in lowered
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    headers = _exception_headers(exc)
    for key, value in headers.items():
        normalized = key.lower()
        if normalized in {"retry-after", "x-ratelimit-reset", "x-rate-limit-reset"}:
            parsed = _parse_retry_after(value)
            if parsed is not None:
                return parsed
    return None


def _exception_headers(exc: Exception) -> dict[str, str]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        return {str(key): str(value) for key, value in dict(headers).items()}
    return {}


def _parse_retry_after(value: str) -> float | None:
    text = str(value).strip()
    if not text:
        return None
    with contextlib.suppress(ValueError):
        number = float(text)
        if number > 10_000_000_000:
            number = number / 1000
        if number > 1_000_000_000:
            return max(0.0, number - time.time())
        return max(0.0, number)
    with contextlib.suppress(ValueError, TypeError, OSError):
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - time.time())
    return None


def _sleep_or_stop(seconds: float, max_iterations: int | None, iterations: int) -> None:
    if max_iterations is not None and iterations >= max_iterations:
        return
    time.sleep(seconds)


def _focused_job_id(config: AppConfig) -> str | None:
    path = config.runtime.home / "shell_state.json"
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    job_id = parsed.get("focus_job_id") if isinstance(parsed, dict) else None
    return job_id if isinstance(job_id, str) and job_id else None
