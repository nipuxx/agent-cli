from datetime import datetime, timedelta, timezone
import json
import time

import pytest

from nipux_cli.config import AppConfig, RuntimeConfig
from nipux_cli.daemon import (
    Daemon,
    DaemonAlreadyRunning,
    RUNTIME_CODE_FILES,
    append_daemon_event,
    current_runtime_fingerprint,
    daemon_lock_status,
    read_daemon_events,
    runtime_stale,
    single_instance_lock,
    update_lock_metadata,
    _exception_backoff,
    _parse_retry_after,
    _step_failure_backoff,
)
from nipux_cli.db import AgentDB
from nipux_cli.worker import StepExecution


def test_single_instance_lock_rejects_second_holder(tmp_path):
    lock_path = tmp_path / "agentd.lock"
    with single_instance_lock(lock_path):
        with pytest.raises(DaemonAlreadyRunning):
            with single_instance_lock(lock_path):
                pass


def test_daemon_lock_status_reports_free_lock(tmp_path):
    status = daemon_lock_status(tmp_path / "agentd.lock")

    assert status["running"] is False
    assert status["detail"] == "daemon lock is free"


def test_lock_metadata_can_be_updated_while_held(tmp_path):
    lock_path = tmp_path / "agentd.lock"
    with single_instance_lock(lock_path) as handle:
        update_lock_metadata(handle, last_state="step", consecutive_failures=2)
        status = daemon_lock_status(lock_path)

    assert status["running"] is True
    assert status["metadata"]["last_state"] == "step"
    assert status["metadata"]["consecutive_failures"] == 2


def test_daemon_lock_status_detects_stale_runtime(tmp_path):
    lock_path = tmp_path / "agentd.lock"
    with single_instance_lock(lock_path) as handle:
        update_lock_metadata(handle, runtime={"runtime_hash": "old"})
        status = daemon_lock_status(lock_path)

    assert status["running"] is True
    assert status["stale"] is True
    assert status["current_runtime"]["code_hash"]
    assert status["current_runtime"]["code_mtime"]
    assert runtime_stale({"runtime": {"runtime_hash": "old"}}) is True
    assert runtime_stale({"runtime": current_runtime_fingerprint()}) is False


def test_runtime_fingerprint_tracks_progress_code():
    assert "progress.py" in RUNTIME_CODE_FILES


def test_rate_limit_backoff_uses_retry_after_header():
    class RateLimit(Exception):
        status_code = 429
        response = type("Response", (), {"headers": {"Retry-After": "42"}})()

    assert _exception_backoff(RateLimit("too many requests"), poll_seconds=0, consecutive_failures=1) == 42


def test_rate_limit_backoff_has_conservative_fallback():
    class RateLimit(Exception):
        status_code = 429

    assert _exception_backoff(RateLimit("rate limit exceeded"), poll_seconds=0, consecutive_failures=1) == 10


def test_failed_step_provider_config_error_gets_long_backoff():
    result = StepExecution(
        job_id="job",
        run_id="run",
        step_id="step",
        tool_name=None,
        status="failed",
        result={
            "error_type": "PermissionDeniedError",
            "error": "Error code: 403 - key limit exceeded",
        },
    )

    assert _step_failure_backoff(result, poll_seconds=0, consecutive_failures=1) == 300


def test_failed_step_rate_limit_gets_throttled_backoff():
    result = StepExecution(
        job_id="job",
        run_id="run",
        step_id="step",
        tool_name=None,
        status="failed",
        result={
            "error_type": "RateLimitError",
            "error": "429 too many requests",
        },
    )

    assert _step_failure_backoff(result, poll_seconds=0, consecutive_failures=1) == 60


def test_retry_after_parses_epoch_milliseconds():
    future_ms = str(int((time.time() + 5) * 1000))

    parsed = _parse_retry_after(future_ms)

    assert parsed is not None
    assert 0 <= parsed <= 6


def test_daemon_run_once_claims_next_job_with_fake_step(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run forever in small steps")
        daemon = Daemon(config=config, db=db)

        result = daemon.run_once(fake=True)

        assert result is not None
        assert result.job_id == job_id
        assert result.status == "completed"
        assert db.list_artifacts(job_id)[0]["title"] == "daemon-fake-step"
    finally:
        db.close()


def test_daemon_ignores_ui_focus_for_worker_scheduling(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        first = db.create_job("First job", title="first")
        second = db.create_job("Second job", title="second")
        (tmp_path / "shell_state.json").write_text(json.dumps({"focus_job_id": second}), encoding="utf-8")
        daemon = Daemon(config=config, db=db)

        job = daemon.next_runnable_job()

        assert first != second
        assert job is not None
        assert job["id"] == first
    finally:
        db.close()


def test_daemon_skips_deferred_jobs_until_due(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        deferred = db.create_job("Deferred job", title="deferred")
        ready = db.create_job("Ready job", title="ready")
        db.update_job_status(
            deferred,
            "queued",
            metadata_patch={"defer_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        )
        daemon = Daemon(config=config, db=db)

        job = daemon.next_runnable_job()

        assert job is not None
        assert job["id"] == ready
    finally:
        db.close()


def test_daemon_idle_sleep_wakes_for_deferred_job(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        now = datetime.now(timezone.utc)
        job_id = db.create_job("Deferred job", title="deferred")
        db.update_job_status(
            job_id,
            "queued",
            metadata_patch={"defer_until": (now + timedelta(seconds=2)).isoformat()},
        )
        daemon = Daemon(config=config, db=db)

        sleep_seconds = daemon.idle_sleep_seconds(poll_seconds=30, now=now)

        assert 1.9 <= sleep_seconds <= 2.1
    finally:
        db.close()


def test_daemon_idle_sleep_uses_poll_when_no_deferred_jobs(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        db.create_job("Ready job", title="ready")
        daemon = Daemon(config=config, db=db)

        assert daemon.idle_sleep_seconds(poll_seconds=30) == 30
        assert daemon.idle_sleep_seconds(poll_seconds=0) == 5.0
    finally:
        db.close()


def test_daemon_advances_multiple_runnable_jobs_without_focus_starvation(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path, daily_digest_enabled=False))
    db = AgentDB(tmp_path / "state.db")
    try:
        first = db.create_job("First job", title="first")
        second = db.create_job("Second job", title="second")
        (tmp_path / "shell_state.json").write_text(json.dumps({"focus_job_id": second}), encoding="utf-8")
        daemon = Daemon(config=config, db=db)

        daemon.run_forever(poll_seconds=0, quiet=True, max_iterations=4, fake=True)

        assert db.list_steps(job_id=first)
        assert db.list_steps(job_id=second)
    finally:
        db.close()


def test_daemon_writes_due_daily_digest_once(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path, daily_digest_time="00:00"))
    db = AgentDB(tmp_path / "state.db")
    try:
        db.create_job("Keep finding findings", title="findings")
        daemon = Daemon(config=config, db=db)
        now = datetime(2026, 4, 23, 8, 30)

        first = daemon.send_due_daily_digest(now=now)
        second = daemon.send_due_daily_digest(now=now)

        assert first is not None
        assert first["status"] == "dry_run"
        assert second is None
        assert (tmp_path / "digests" / "2026-04-23-daily.md").exists()
    finally:
        db.close()


def test_daemon_event_log_round_trips_jsonl(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))

    path = append_daemon_event(config, "step", job_id="job_1", status="completed")
    events = read_daemon_events(config, limit=3)

    assert path.name == "daemon-events.jsonl"
    assert events[-1]["event"] == "step"
    assert events[-1]["job_id"] == "job_1"


def test_daemon_recovers_stale_running_steps_on_start(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path, daily_digest_enabled=False))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover stale work", title="stale")
        run_id = db.start_run(job_id, model="fake")
        stale_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="browser_navigate")
        daemon = Daemon(config=config, db=db)

        daemon.run_forever(poll_seconds=0, quiet=True, max_iterations=1, fake=True)

        steps = db.list_steps(job_id=job_id)
        stale = next(step for step in steps if step["id"] == stale_step)
        events = read_daemon_events(config, limit=5)
        assert stale["status"] == "failed"
        assert stale["error"] == "daemon recovered abandoned running work from a previous process"
        assert db.list_runs(job_id, limit=10)[-1]["status"] == "failed"
        assert any(event.get("event") == "stale_work_recovered" for event in events)
    finally:
        db.close()


def test_daemon_survives_unexpected_step_exception(tmp_path):
    class ExplodingDaemon(Daemon):
        def run_once(self, *, fake: bool = False, verbose: bool = False):  # noqa: ARG002
            raise RuntimeError("provider fell over")

    config = AppConfig(runtime=RuntimeConfig(home=tmp_path, daily_digest_enabled=False))
    db = AgentDB(tmp_path / "state.db")
    try:
        daemon = ExplodingDaemon(config=config, db=db)

        daemon.run_forever(poll_seconds=0, quiet=True, max_iterations=1)

        status = daemon_lock_status(tmp_path / "agentd.lock")
        events = read_daemon_events(config, limit=5)
        assert status["metadata"]["last_state"] == "error"
        assert status["metadata"]["consecutive_failures"] == 1
        assert any(event.get("event") == "daemon_error" for event in events)
    finally:
        db.close()


def test_daemon_treats_blocked_steps_as_recoverable(tmp_path):
    class BlockedDaemon(Daemon):
        def run_once(self, *, fake: bool = False, verbose: bool = False):  # noqa: ARG002
            return StepExecution(
                job_id="job",
                run_id="run",
                step_id="step",
                tool_name="web_search",
                status="blocked",
                result={"error": "search loop blocked", "recoverable": True},
            )

    config = AppConfig(runtime=RuntimeConfig(home=tmp_path, daily_digest_enabled=False))
    db = AgentDB(tmp_path / "state.db")
    try:
        daemon = BlockedDaemon(config=config, db=db)

        daemon.run_forever(poll_seconds=0, quiet=True, max_iterations=3)

        status = daemon_lock_status(tmp_path / "agentd.lock")
        events = read_daemon_events(config, limit=10)
        assert status["metadata"]["consecutive_failures"] == 0
        assert sum(1 for event in events if event.get("event") == "step") == 3
        assert not any(event.get("event") == "daemon_error" for event in events)
    finally:
        db.close()


def test_fake_daemon_can_run_100_iterations_without_auto_stop(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path, daily_digest_enabled=False))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run a long fake worker", title="long")
        daemon = Daemon(config=config, db=db)

        daemon.run_forever(poll_seconds=0, quiet=True, max_iterations=100, fake=True)

        steps = db.list_steps(job_id=job_id)
        assert len(steps) == 100
        assert any(step["kind"] == "reflection" for step in steps)
        assert db.list_artifacts(job_id)
        assert daemon_lock_status(tmp_path / "agentd.lock")["running"] is False
    finally:
        db.close()
