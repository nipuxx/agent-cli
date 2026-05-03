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
    assert "+1 finding" in checkpoint.message
    assert "+1 source" in checkpoint.message
    assert "+1 experiment" in checkpoint.message
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


def test_progress_checkpoint_without_delta_is_activity_not_progress():
    metadata = {"finding_ledger": [{}], "source_ledger": [{}], "task_queue": [{}], "experiment_ledger": []}

    checkpoint = build_progress_checkpoint(
        metadata,
        previous_counts={"findings": 1, "sources": 1, "tasks": 1, "experiments": 0, "lessons": 0, "milestones": 0},
        step_no=50,
        tool_name="web_extract",
    )

    assert checkpoint.category == "activity"
    assert "no new durable ledger entries" in checkpoint.message


def test_progress_checkpoint_counts_existing_record_updates_as_progress():
    metadata = {
        "last_checkpoint_at": "2026-01-01T00:00:00+00:00",
        "finding_ledger": [{}],
        "source_ledger": [{}],
        "task_queue": [{"title": "Existing branch", "status": "done"}],
        "experiment_ledger": [{"title": "Trial", "status": "measured"}],
        "last_task_record": {
            "title": "Existing branch",
            "status": "done",
            "result": "Validated the branch.",
            "created": False,
            "updated_at": "2026-01-01T00:01:00+00:00",
        },
        "last_source_record": {
            "source": "https://example.test",
            "created": False,
            "last_seen": "2026-01-01T00:01:30+00:00",
        },
        "last_experiment_record": {
            "title": "Trial",
            "status": "measured",
            "metric_name": "score",
            "metric_value": 0.9,
            "created": False,
            "updated_at": "2026-01-01T00:02:00+00:00",
        },
    }

    checkpoint = build_progress_checkpoint(
        metadata,
        previous_counts={"findings": 1, "sources": 1, "tasks": 1, "experiments": 1, "lessons": 0, "milestones": 0},
        step_no=60,
        tool_name="record_tasks",
    )

    assert checkpoint.category == "progress"
    assert checkpoint.deltas["tasks"] == 0
    assert checkpoint.updates["tasks"] == 1
    assert checkpoint.updates["sources"] == 1
    assert checkpoint.resolutions["tasks"] == 1
    assert checkpoint.updates["experiments"] == 1
    assert checkpoint.resolutions["experiments"] == 1
    assert "~1 task updated" in checkpoint.message
    assert "~1 source updated" in checkpoint.message
    assert "1 task resolved" in checkpoint.message
    assert "~1 experiment updated" in checkpoint.message


def test_progress_checkpoint_counts_roadmap_updates_and_validations():
    metadata = {
        "last_checkpoint_at": "2026-01-01T00:00:00+00:00",
        "roadmap": {"milestones": [{"title": "Foundation", "status": "validating"}]},
        "last_roadmap_record": {
            "title": "Roadmap",
            "created": False,
            "updated_at": "2026-01-01T00:01:00+00:00",
            "added_milestones": 0,
            "updated_milestones": 1,
            "added_features": 0,
            "updated_features": 0,
        },
        "last_milestone_validation": {
            "milestone": "Foundation",
            "validation_status": "passed",
            "validated_at": "2026-01-01T00:02:00+00:00",
        },
    }

    checkpoint = build_progress_checkpoint(
        metadata,
        previous_counts={"findings": 0, "sources": 0, "tasks": 0, "experiments": 0, "lessons": 0, "milestones": 1},
        step_no=70,
        tool_name="record_milestone_validation",
    )

    assert checkpoint.category == "progress"
    assert checkpoint.deltas["milestones"] == 0
    assert checkpoint.updates["milestones"] == 2
    assert checkpoint.resolutions["milestones"] == 1
    assert "~2 milestones updated" in checkpoint.message
    assert "1 milestone resolved" in checkpoint.message


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
