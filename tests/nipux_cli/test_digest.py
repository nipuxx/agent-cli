from nipux_cli.config import AppConfig, RuntimeConfig
from nipux_cli.db import AgentDB
from nipux_cli.digest import render_daily_digest, write_daily_digest


def test_daily_digest_includes_ledgers_lessons_sources_and_strategy(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research", kind="generic")
        db.append_finding_record(job_id, name="Acme Finding", category="example category", reason="reusable result", score=0.8)
        db.append_task_record(job_id, title="Explore primary sources", status="open", priority=5)
        db.append_source_record(job_id, "https://example.com", usefulness_score=0.9, yield_count=1, outcome="yielded findings")
        db.append_lesson(job_id, "Low-evidence pages are not finding sources.", category="source_quality")
        db.append_reflection(job_id, "Directories are working.", strategy="Try chambers next.")

        body = render_daily_digest(db)
        result = write_daily_digest(config, db, day="2026-04-25")

        assert "Counts: 1 findings, 1 sources, 1 tasks, 0 experiments, 1 lessons" in body
        assert "Experiments:" in body
        assert "Acme Finding" in body
        assert "Explore primary sources" in body
        assert "Low-evidence pages are not finding sources." in body
        assert "https://example.com" in body
        assert "Try chambers next." in body
        assert result["status"] == "dry_run"
        assert (tmp_path / "digests" / "2026-04-25-daily.md").exists()
    finally:
        db.close()
