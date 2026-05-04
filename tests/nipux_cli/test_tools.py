import json

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, RuntimeConfig, ToolAccessConfig
from nipux_cli.db import AgentDB
from nipux_cli.tools import APPROVED_TOOL_NAMES, DEFAULT_REGISTRY, ToolContext


def test_static_tool_surface_is_focused():
    assert tuple(DEFAULT_REGISTRY.names()) == tuple(sorted(APPROVED_TOOL_NAMES))
    assert "terminal" not in DEFAULT_REGISTRY.names()
    assert "delegate_task" not in DEFAULT_REGISTRY.names()
    assert "skill_manage" not in DEFAULT_REGISTRY.names()
    assert "browser_navigate" in DEFAULT_REGISTRY.names()
    assert "shell_exec" in DEFAULT_REGISTRY.names()
    assert "write_file" in DEFAULT_REGISTRY.names()
    assert "write_artifact" in DEFAULT_REGISTRY.names()
    assert "defer_job" in DEFAULT_REGISTRY.names()
    assert "report_update" in DEFAULT_REGISTRY.names()
    assert "record_lesson" in DEFAULT_REGISTRY.names()
    assert "record_source" in DEFAULT_REGISTRY.names()
    assert "record_findings" in DEFAULT_REGISTRY.names()
    assert "record_tasks" in DEFAULT_REGISTRY.names()
    assert "record_roadmap" in DEFAULT_REGISTRY.names()
    assert "record_milestone_validation" in DEFAULT_REGISTRY.names()
    assert "record_experiment" in DEFAULT_REGISTRY.names()
    assert "acknowledge_operator_context" in DEFAULT_REGISTRY.names()


def test_tool_access_config_filters_worker_schema_and_blocks_calls(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path), tools=ToolAccessConfig(browser=False, web=False, shell=False, files=False))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Restricted tools")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id)

        names = {tool["function"]["name"] for tool in DEFAULT_REGISTRY.openai_tools(config=config)}
        assert "browser_navigate" not in names
        assert "web_search" not in names
        assert "shell_exec" not in names
        assert "write_file" not in names
        assert "write_artifact" in names

        result = json.loads(DEFAULT_REGISTRY.handle("shell_exec", {"command": "printf no"}, ctx))
        assert result["success"] is False
        assert result["tool_access"] == "shell"
    finally:
        db.close()


def test_artifact_tools_roundtrip(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Save evidence")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle("write_artifact", {"content": "needle text", "title": "Evidence"}, ctx)
        result = json.loads(raw)
        assert result["success"] is True

        read_raw = DEFAULT_REGISTRY.handle("read_artifact", {"artifact_id": result["artifact_id"]}, ctx)
        assert json.loads(read_raw)["content"] == "needle text"

        path_raw = DEFAULT_REGISTRY.handle("read_artifact", {"artifact_id": result["path"]}, ctx)
        assert json.loads(path_raw)["artifact_id"] == result["artifact_id"]

        title_raw = DEFAULT_REGISTRY.handle("read_artifact", {"title": "Evidence"}, ctx)
        assert json.loads(title_raw)["content"] == "needle text"

        number_raw = DEFAULT_REGISTRY.handle("read_artifact", {"artifact_id": "1"}, ctx)
        assert json.loads(number_raw)["content"] == "needle text"

        search_raw = DEFAULT_REGISTRY.handle("search_artifacts", {"query": "needle"}, ctx)
        assert json.loads(search_raw)["results"][0]["id"] == result["artifact_id"]
    finally:
        db.close()


def test_defer_job_records_resume_time_without_pausing(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Monitor a long process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="defer_job")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "defer_job",
            {"seconds": 60, "reason": "process is still running", "next_action": "check status"},
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is True
        assert result["status"] == "running"
        job = db.get_job(job_id)
        assert job["status"] == "running"
        assert job["metadata"]["defer_until"]
        assert job["metadata"]["defer_reason"] == "process is still running"
        assert job["metadata"]["defer_next_action"] == "check status"
        assert any(event["event_type"] == "agent_message" for event in db.list_events(job_id=job_id, limit=10))
    finally:
        db.close()


def test_shell_exec_tool_runs_bounded_command(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle("shell_exec", {"command": "printf hello", "timeout_seconds": 5}, ctx)
        result = json.loads(raw)

        assert result["success"] is True
        assert result["returncode"] == 0
        assert result["stdout"] == "hello"
    finally:
        db.close()


def test_write_file_tool_writes_and_appends_workspace_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Write deliverable")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_file")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle("write_file", {"path": "out/report.md", "content": "one\n"}, ctx)
        result = json.loads(raw)
        append_raw = DEFAULT_REGISTRY.handle(
            "write_file",
            {"path": "out/report.md", "content": "two\n", "mode": "append"},
            ctx,
        )
        append_result = json.loads(append_raw)

        assert result["success"] is True
        assert append_result["success"] is True
        assert (tmp_path / "out" / "report.md").read_text() == "one\ntwo\n"
    finally:
        db.close()


def test_shell_exec_timeout_kills_process_group(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle("shell_exec", {"command": "sleep 5 | cat", "timeout_seconds": 1}, ctx)
        result = json.loads(raw)

        assert result["success"] is False
        assert result["timed_out"] is True
        assert result["duration_seconds"] < 4
    finally:
        db.close()


def test_shell_exec_does_not_attach_local_ssh_config(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle("shell_exec", {"command": "ssh -V", "timeout_seconds": 5}, ctx)
        result = json.loads(raw)

        assert "ssh_config" not in result
    finally:
        db.close()


def test_shell_exec_reports_nonzero_stderr_as_error(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {"command": "printf 'sudo: a terminal is required to read the password\\n' >&2; exit 1", "timeout_seconds": 5},
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert "interactive sudo/password" in result["error"]
    finally:
        db.close()


def test_update_job_state_keeps_terminal_statuses_operator_only(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep running")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="update_job_state")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        for requested in ("paused", "cancelled", "completed", "failed"):
            raw = DEFAULT_REGISTRY.handle("update_job_state", {"status": requested}, ctx)
            result = json.loads(raw)

            assert result["success"] is True
            assert result["requested_status"] == requested
            assert result["kept_running"] is True
            assert db.get_job(job_id)["status"] == "running"
            if requested == "completed":
                assert result["follow_up_task"]["title"] == "Audit latest checkpoint against objective"
                assert result["follow_up_task"]["status"] == "open"
                assert result["follow_up_task"]["output_contract"] == "decision"
                assert "prompt-to-artifact checklist" in result["follow_up_task"]["acceptance_criteria"]
                assert result["follow_up_task"]["evidence_needed"]
                assert result["follow_up_task"]["stall_behavior"]
                assert result["follow_up_task"]["metadata"]["source"] == "update_job_state"
                assert result["follow_up_task"]["metadata"]["completion_audit_required"] is True
            else:
                assert "follow_up_task" not in result

        tasks = db.get_job(job_id)["metadata"]["task_queue"]
        assert [task["title"] for task in tasks] == ["Audit latest checkpoint against objective"]
    finally:
        db.close()


def test_report_update_tool_records_operator_visible_note(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="report_update")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle("report_update", {"message": "Found a usable finding source", "category": "finding"}, ctx)
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is True
        assert job["metadata"]["agent_updates"][-1]["message"] == "Found a usable finding source"
        assert job["metadata"]["last_agent_update"]["category"] == "finding"
    finally:
        db.close()


def test_record_lesson_tool_records_durable_learning(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_lesson")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_lesson",
            {"lesson": "Competitor low-evidence lists are not finding sources.", "category": "source_quality", "confidence": 0.8},
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is True
        assert job["metadata"]["lessons"][-1]["lesson"] == "Competitor low-evidence lists are not finding sources."
        assert job["metadata"]["last_lesson"]["category"] == "source_quality"
    finally:
        db.close()


def test_record_source_and_findings_tools_update_ledgers(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        source_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_source")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=source_step)

        source_raw = DEFAULT_REGISTRY.handle(
            "record_source",
            {"source": "https://example.com", "source_type": "web_source", "usefulness_score": 0.8, "yield_count": 2},
            ctx,
        )
        finding_raw = DEFAULT_REGISTRY.handle(
            "record_findings",
            {
                "findings": [
                    {
                        "name": "Acme Finding",
                        "url": "https://acme.example",
                        "source_url": "https://example-source.com/acme",
                        "location": "Toronto",
                        "category": "example category",
                        "reason": "reusable result",
                        "score": 0.75,
                    }
                ]
            },
            ctx,
        )
        job = db.get_job(job_id)

        assert json.loads(source_raw)["source"]["yield_count"] == 2
        finding_result = json.loads(finding_raw)
        assert finding_result["added"] == 1
        assert finding_result["sources_updated"] == 1
        assert job["metadata"]["source_ledger"][0]["source"] == "https://example.com"
        assert any(source["source"] == "https://example-source.com/acme" for source in job["metadata"]["source_ledger"])
        assert job["metadata"]["finding_ledger"][0]["name"] == "Acme Finding"
        assert job["metadata"]["last_agent_update"]["category"] == "finding"
    finally:
        db.close()


def test_record_tasks_tool_updates_task_queue(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [
                    {
                        "title": "Explore primary sources",
                        "status": "open",
                        "priority": 5,
                        "goal": "Find artifact-backed evidence",
                        "source_hint": "official docs",
                    }
                ]
            },
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is True
        assert result["added"] == 1
        assert job["metadata"]["task_queue"][0]["title"] == "Explore primary sources"
        assert job["metadata"]["task_queue"][0]["priority"] == 5
        assert job["metadata"]["last_agent_update"]["category"] == "plan"
    finally:
        db.close()


def test_record_roadmap_tool_updates_roadmap(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Build a broad generic outcome")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_roadmap")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_roadmap",
            {
                "title": "Generic Roadmap",
                "status": "active",
                "scope": "Coordinate broad work through milestones.",
                "current_milestone": "Foundation",
                "validation_contract": "Each milestone needs evidence.",
                "milestones": [{
                    "title": "Foundation",
                    "status": "active",
                    "priority": 7,
                    "acceptance_criteria": "first durable output exists",
                    "evidence_needed": "artifact and ledger update",
                    "features": [{
                        "title": "Create first checkpoint",
                        "status": "active",
                        "output_contract": "artifact",
                    }],
                }],
            },
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)
        roadmap = job["metadata"]["roadmap"]

        assert result["success"] is True
        assert roadmap["title"] == "Generic Roadmap"
        assert roadmap["status"] == "active"
        assert roadmap["milestones"][0]["title"] == "Foundation"
        assert roadmap["milestones"][0]["features"][0]["title"] == "Create first checkpoint"
        assert job["metadata"]["last_agent_update"]["metadata"]["roadmap_status"] == "active"
    finally:
        db.close()


def test_record_roadmap_dedupes_milestone_titles_even_when_keys_change(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep broad work coordinated")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_roadmap")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        DEFAULT_REGISTRY.handle(
            "record_roadmap",
            {
                "title": "Generic Roadmap",
                "milestones": [{
                    "key": "initial-key",
                    "title": "Foundation",
                    "status": "planned",
                    "features": [{"key": "feature-a", "title": "First feature", "status": "planned"}],
                }],
            },
            ctx,
        )
        DEFAULT_REGISTRY.handle(
            "record_roadmap",
            {
                "title": "Generic Roadmap",
                "milestones": [{
                    "key": "model-invented-key",
                    "title": "Foundation",
                    "status": "active",
                    "features": [{"key": "different-feature-key", "title": "First feature", "status": "done"}],
                }],
            },
            ctx,
        )
        roadmap = db.get_job(job_id)["metadata"]["roadmap"]

        assert len(roadmap["milestones"]) == 1
        assert roadmap["milestones"][0]["status"] == "active"
        assert len(roadmap["milestones"][0]["features"]) == 1
        assert roadmap["milestones"][0]["features"][0]["status"] == "done"
    finally:
        db.close()


def test_record_milestone_validation_creates_follow_up_tasks(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Validate broad work")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_milestone_validation")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_milestone_validation",
            {
                "milestone": "Foundation",
                "validation_status": "failed",
                "result": "Missing durable evidence.",
                "issues": ["no artifact"],
                "next_action": "Create evidence.",
                "follow_up_tasks": [{
                    "title": "Produce missing evidence",
                    "output_contract": "artifact",
                    "acceptance_criteria": "saved output exists",
                }],
            },
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)
        roadmap = job["metadata"]["roadmap"]

        assert result["success"] is True
        assert result["validation"]["validation_status"] == "failed"
        assert result["follow_up_tasks"][0]["title"] == "Produce missing evidence"
        assert roadmap["milestones"][0]["status"] == "blocked"
        assert job["metadata"]["task_queue"][0]["parent"] == "Foundation"
        assert job["metadata"]["last_agent_update"]["metadata"]["validation_status"] == "failed"
    finally:
        db.close()


def test_record_experiment_tool_tracks_best_measured_result(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        first = DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "baseline attempt",
                "status": "measured",
                "metric_name": "score",
                "metric_value": 2.0,
                "metric_unit": "units",
                "higher_is_better": True,
                "config": {"variant": "a"},
                "result": "baseline measured",
                "next_action": "try variant b",
            },
            ctx,
        )
        second = DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "second attempt",
                "status": "measured",
                "metric_name": "score",
                "metric_value": 3.5,
                "metric_unit": "units",
                "higher_is_better": True,
                "config": {"variant": "b"},
                "result": "improved",
                "next_action": "test a different branch",
            },
            ctx,
        )
        job = db.get_job(job_id)
        experiments = job["metadata"]["experiment_ledger"]

        assert json.loads(first)["experiment"]["best_observed"] is True
        assert json.loads(second)["experiment"]["best_observed"] is True
        assert experiments[0]["best_observed"] is False
        assert experiments[1]["best_observed"] is True
        assert experiments[1]["delta_from_previous_best"] == 1.5
        assert job["metadata"]["best_experiment_record"]["title"] == "second attempt"
        assert job["metadata"]["last_agent_update"]["metadata"]["best_observed"] is True
    finally:
        db.close()


def test_acknowledge_operator_context_tool_marks_context(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run with operator corrections")
        entry = db.append_operator_message(job_id, "use the corrected target", source="chat")
        db.claim_operator_messages(job_id, modes=("steer",), limit=1)
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="acknowledge_operator_context")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "acknowledge_operator_context",
            {"message_ids": [entry["event_id"]], "summary": "correction incorporated"},
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is True
        assert result["count"] == 1
        assert job["metadata"]["operator_messages"][0]["acknowledged_at"]
        assert job["metadata"]["last_operator_context_ack"]["summary"] == "correction incorporated"
    finally:
        db.close()


def test_record_tasks_accepts_generic_output_contracts(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Run one comparison",
                    "status": "open",
                    "output_contract": "experiment",
                    "acceptance_criteria": "metric recorded",
                    "evidence_needed": "command output or artifact",
                    "stall_behavior": "record blocker and pivot",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert result["success"] is True
        assert task["output_contract"] == "experiment"
        assert task["acceptance_criteria"] == "metric recorded"
        assert task["evidence_needed"] == "command output or artifact"
        assert task["stall_behavior"] == "record blocker and pivot"
    finally:
        db.close()


def test_record_tasks_downgrades_done_artifact_without_delivery_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Update a deliverable")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Update report draft",
                    "status": "done",
                    "output_contract": "artifact",
                    "result": "Updated the report",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert result["success"] is True
        assert task["status"] == "active"
        assert task["metadata"]["completion_validation"] == "missing_recent_deliverable_evidence"
        assert task["metadata"]["claimed_result"] == "Updated the report"
    finally:
        db.close()


def test_record_tasks_downgrades_done_without_result_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Validate generic work")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Check current branch",
                    "status": "done",
                    "output_contract": "decision",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert result["success"] is True
        assert task["status"] == "active"
        assert task["metadata"]["completion_validation"] == "missing_result_evidence"
    finally:
        db.close()


def test_record_tasks_allows_done_artifact_after_delivery_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Update a deliverable")
        run_id = db.start_run(job_id, model="fake")
        artifact_step = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="write_artifact",
            input_data={"arguments": {"title": "Final report draft", "summary": "Updated report deliverable"}},
        )
        db.finish_step(artifact_step, status="completed", summary="write_artifact saved art_demo")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Update report draft",
                    "status": "done",
                    "output_contract": "artifact",
                    "result": "Saved final report draft",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert result["success"] is True
        assert task["status"] == "done"
        assert "completion_validation" not in task.get("metadata", {})
    finally:
        db.close()


def test_record_tasks_does_not_treat_stderr_redirect_as_delivery_write(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Update a deliverable")
        run_id = db.start_run(job_id, model="fake")
        shell_step = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="shell_exec",
            input_data={"arguments": {"command": "cat draft.md 2>/dev/null"}},
        )
        db.finish_step(shell_step, status="completed", summary="shell_exec rc=0")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Update report draft",
                    "status": "done",
                    "output_contract": "artifact",
                    "result": "Saved final report draft",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert result["success"] is True
        assert task["status"] == "active"
        assert task["metadata"]["completion_validation"] == "missing_recent_deliverable_evidence"
    finally:
        db.close()


def test_record_tasks_rejects_checkpoint_as_delivery_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Update a deliverable")
        run_id = db.start_run(job_id, model="fake")
        artifact_step = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="write_artifact",
            input_data={"arguments": {"title": "Compiled report checkpoint", "summary": "Checkpoint before final rewrite"}},
        )
        db.finish_step(artifact_step, status="completed", summary="write_artifact saved art_demo")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Update report draft",
                    "status": "done",
                    "output_contract": "artifact",
                    "result": "Saved final report draft",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert result["success"] is True
        assert task["status"] == "active"
        assert task["metadata"]["completion_validation"] == "missing_recent_deliverable_evidence"
    finally:
        db.close()
