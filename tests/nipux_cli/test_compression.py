from nipux_cli.compression import refresh_memory_index
from nipux_cli.db import AgentDB


def test_refresh_memory_index_includes_durable_progress_ledgers(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Keep improving a report",
            title="report",
            metadata={
                "task_queue": [
                    {
                        "title": "Draft evidence-backed section",
                        "status": "active",
                        "priority": 10,
                        "output_contract": "report",
                    }
                ],
                "finding_ledger": [{"name": "Teacher traces improve tool use"}],
                "source_ledger": [{"source": "https://example.test/paper", "usefulness_score": 0.8}],
                "experiment_ledger": [
                    {
                        "title": "Citation density check",
                        "status": "measured",
                        "metric_name": "citations",
                        "metric_value": 12,
                        "metric_unit": "count",
                    }
                ],
                "roadmap": {
                    "title": "Research paper roadmap",
                    "status": "active",
                    "current_milestone": "Improve literature review",
                },
            },
        )

        refresh_memory_index(db, job_id)

        memory = db.list_memory(job_id)[0]["summary"]
        assert "Durable progress ledgers:" in memory
        assert "tasks=1" in memory
        assert "findings=1" in memory
        assert "sources=1" in memory
        assert "experiments=1" in memory
        assert "Draft evidence-backed section" in memory
        assert "Citation density check" in memory
        assert "Teacher traces improve tool use" in memory
        assert "Research paper roadmap" in memory
    finally:
        db.close()
