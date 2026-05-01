from nipux_cli.progress import build_progress_checkpoint, ledger_counts, recent_progress_bits


def test_progress_checkpoint_reports_deltas_and_recent_durable_work():
    metadata = {
        "finding_ledger": [{"title": "First finding"}, {"title": "Better branch"}],
        "source_ledger": [{"url": "https://example.test"}],
        "task_queue": [
            {"title": "Draft report", "status": "done", "priority": 2},
            {"title": "Validate report", "status": "open", "priority": 8},
        ],
        "experiment_ledger": [{"title": "Quality check", "metric_name": "score", "metric_value": 0.82}],
        "lessons": [{"lesson": "Prefer measured output"}],
        "roadmap": {"milestones": [{"title": "Publishable draft", "status": "validating"}]},
    }

    checkpoint = build_progress_checkpoint(
        metadata,
        previous_counts={"findings": 1, "sources": 0, "tasks": 2, "experiments": 0, "lessons": 1, "milestones": 0},
        step_no=40,
        tool_name="record_findings",
    )

    assert checkpoint.counts == {
        "findings": 2,
        "sources": 1,
        "tasks": 2,
        "experiments": 1,
        "lessons": 1,
        "milestones": 1,
    }
    assert checkpoint.deltas["findings"] == 1
    assert checkpoint.deltas["sources"] == 1
    assert checkpoint.deltas["tasks"] == 0
    assert checkpoint.category == "progress"
    assert "+1 findings" in checkpoint.message
    assert "+1 sources" in checkpoint.message
    assert "+1 experiments" in checkpoint.message
    assert "finding=Better branch" in checkpoint.message
    assert "task=Validate report" in checkpoint.message
    assert "measurement=score=0.82" in checkpoint.message
    assert "milestone=Publishable draft" in checkpoint.message


def test_progress_checkpoint_for_saved_output_is_concise():
    metadata = {"finding_ledger": [{}], "source_ledger": [{}, {}], "task_queue": [{}], "experiment_ledger": []}

    checkpoint = build_progress_checkpoint(
        metadata,
        step_no=12,
        tool_name="write_artifact",
        artifact_id="art_123",
        is_finding_output=True,
    )

    assert checkpoint.category == "finding"
    assert checkpoint.message.startswith("Saved output art_123")
    assert "1 findings, 2 sources, 1 tasks, and 0 experiments" in checkpoint.message


def test_progress_helpers_ignore_malformed_metadata():
    metadata = {
        "finding_ledger": "bad",
        "source_ledger": [None, {"url": "ok"}],
        "task_queue": [{"title": "Task", "status": "blocked", "priority": "bad"}],
        "roadmap": {"milestones": ["bad", {"title": "Milestone", "status": "active"}]},
    }

    assert ledger_counts(metadata)["sources"] == 1
    assert ledger_counts(metadata)["milestones"] == 2
    bits = recent_progress_bits(metadata)
    assert "task=Task" in bits
    assert "milestone=Milestone" in bits
