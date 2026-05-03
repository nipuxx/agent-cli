from datetime import datetime, timedelta, timezone

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, RuntimeConfig
from nipux_cli.dashboard import collect_dashboard_state, render_dashboard, render_overview
from nipux_cli.db import AgentDB


def test_dashboard_collects_jobs_steps_and_artifacts(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic every morning", title="research", kind="generic")
        run_id = db.start_run(job_id, model="fake-model")
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="write_artifact",
            input_data={"arguments": {"title": "Findings"}},
        )
        ArtifactStore(tmp_path, db=db).write_text(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            title="Findings",
            summary="first saved finding",
            content="Acme Corp",
        )
        db.finish_step(step_id, status="completed", summary="saved finding", output_data={"success": True})
        db.finish_run(run_id, "completed")
        db.append_lesson(job_id, "Low-evidence summaries are not finding batches.", category="source_quality")
        db.append_task_record(job_id, title="Explore primary sources", status="open", priority=5)

        state = collect_dashboard_state(db, config, job_id=job_id)
        rendered = render_dashboard(state, width=100)
        overview = render_overview(state, width=100)

        assert state["daemon"]["running"] is False
        assert state["focus"]["counts"]["artifacts"] == 1
        assert state["focus"]["counts"]["tasks"] == 1
        assert "Nipux CLI Dashboard" in rendered
        assert "research" in rendered
        assert "write_artifact" in rendered
        assert "Findings" in rendered
        assert "Low-evidence summaries are not finding batches" in rendered
        assert "Explore primary sources" in rendered
        assert "Nipux Status" in overview
        assert "latest artifact: Findings" in overview
        assert "latest lesson:" in overview
    finally:
        db.close()


def test_overview_marks_idle_daemon_as_ready_for_work(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        db.create_job("Research topic", title="research")
        state = collect_dashboard_state(db, config)
        overview = render_overview(state, width=100)

        assert "ready when work starts" in overview
    finally:
        db.close()


def test_overview_marks_old_heartbeat_as_busy_for_running_step(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Measure a process", title="measure")
        run_id = db.start_run(job_id, model="fake-model")
        db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec", status="running")
        state = collect_dashboard_state(db, config, job_id=job_id)
        state["daemon"]["running"] = True
        state["daemon"]["metadata"] = {
            "last_heartbeat": (datetime.now(timezone.utc) - timedelta(seconds=180)).isoformat(),
        }

        overview = render_overview(state, width=100)

        assert "busy #1 shell_exec" in overview
        assert "heartbeat 180s ago (stale)" not in overview
    finally:
        db.close()
