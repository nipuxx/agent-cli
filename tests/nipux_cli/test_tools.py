import json
import os
import signal
import subprocess
import time

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, RuntimeConfig, ToolAccessConfig
from nipux_cli.db import AgentDB
from nipux_cli.shell_tools import cleanup_registered_shell_processes
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
    assert "record_memory_graph" in DEFAULT_REGISTRY.names()
    assert "search_memory_graph" in DEFAULT_REGISTRY.names()
    assert "record_source" in DEFAULT_REGISTRY.names()
    assert "record_findings" in DEFAULT_REGISTRY.names()
    assert "record_tasks" in DEFAULT_REGISTRY.names()
    assert "record_roadmap" in DEFAULT_REGISTRY.names()
    assert "record_milestone_validation" in DEFAULT_REGISTRY.names()
    assert "record_experiment" in DEFAULT_REGISTRY.names()
    assert "acknowledge_operator_context" in DEFAULT_REGISTRY.names()


def test_tool_registry_validates_required_arguments(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))

    missing = DEFAULT_REGISTRY.validate_arguments("shell_exec", {}, config)
    assert missing is not None
    assert missing["missing_arguments"] == ["command"]
    assert missing["recoverable"] is True

    artifact_ref = DEFAULT_REGISTRY.validate_arguments("read_artifact", {}, config)
    assert artifact_ref is not None
    assert artifact_ref["missing_arguments"] == ["artifact reference"]

    graph = DEFAULT_REGISTRY.validate_arguments("record_memory_graph", {}, config)
    assert graph is not None
    assert graph["missing_arguments"] == ["nodes or edges"]

    experiment = DEFAULT_REGISTRY.validate_arguments("record_experiment", {"metric_name": "throughput"}, config)
    assert experiment is None

    nested = DEFAULT_REGISTRY.validate_arguments("record_findings", {"findings": [{}]}, config)
    assert nested is not None
    assert nested["missing_arguments"] == ["findings[0].name"]

    nested_task = DEFAULT_REGISTRY.validate_arguments("record_tasks", {"tasks": [{"goal": "do work"}]}, config)
    assert nested_task is not None
    assert nested_task["missing_arguments"] == ["tasks[0].title"]


def test_tool_registry_blocks_truncated_reference_arguments(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))

    experiment = DEFAULT_REGISTRY.validate_arguments(
        "record_experiment",
        {
            "title": "Measure local files",
            "evidence_artifact": "art_fb73...",
            "next_action": "validate the exact artifact",
        },
        config,
    )

    assert experiment is not None
    assert experiment["error"] == "placeholder tool arguments"
    assert experiment["placeholder_arguments"] == ["evidence_artifact"]


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


def test_read_artifact_missing_ref_returns_valid_recent_refs(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Save evidence")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)
        stored = ctx.artifacts.write_text(job_id=job_id, run_id=run_id, step_id=step_id, title="Useful Evidence", content="saved")

        raw = DEFAULT_REGISTRY.handle("read_artifact", {"artifact_id": "art_missing"}, ctx)
        result = json.loads(raw)

        assert result["success"] is False
        assert result["recoverable"] is True
        assert result["error"] == "artifact not found: art_missing"
        assert "search_artifacts" in result["guidance"]
        assert result["recent_artifacts"][0]["id"] == stored.id
        assert result["recent_artifacts"][0]["title"] == "Useful Evidence"
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


def test_shell_exec_flags_masked_auth_failure_output(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {
                "command": (
                    "printf 'HTTP request sent, awaiting response... 401 Unauthorized\\n"
                    "Username/Password Authentication Failed.\\nDownloaded: file.bin (29 bytes)\\n'"
                ),
                "timeout_seconds": 5,
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["returncode"] == 0
        assert result["success"] is False
        assert "authentication or authorization failure" in result["error"]
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


def test_cleanup_registered_shell_processes_kills_orphaned_group(tmp_path):
    process = subprocess.Popen("sleep 30", shell=True, start_new_session=True)
    for _ in range(20):
        if process.poll() is None:
            try:
                os.kill(process.pid, 0)
                break
            except ProcessLookupError:
                pass
        time.sleep(0.02)
    registry = tmp_path / "runtime" / "shell_processes.jsonl"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"pid": process.pid, "command": "sleep 30"}) + "\n", encoding="utf-8")
    try:
        cleaned = cleanup_registered_shell_processes(tmp_path)

        assert cleaned and cleaned[0]["pid"] == process.pid
        process.wait(timeout=3)
        assert not registry.exists()
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)


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


def test_shell_exec_flags_sudo_password_hidden_by_success_status(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {
                "command": (
                    "printf 'sudo: a terminal is required to read the password\\n"
                    "sudo: a password is required\\n'"
                ),
                "timeout_seconds": 5,
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["returncode"] == 0
        assert result["success"] is False
        assert "interactive sudo/password" in result["error"]
    finally:
        db.close()


def test_shell_exec_flags_missing_command_hidden_by_success_status(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {"command": "printf '/bin/sh: 1: build-tool: not found\\n'", "timeout_seconds": 5},
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert "missing command" in result["error"]
    finally:
        db.close()


def test_shell_exec_flags_missing_absolute_executable_hidden_by_success_status(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {"command": "printf '/bin/sh: 1: /tmp/tools/build-tool: not found\\n'", "timeout_seconds": 5},
            ctx,
        )
        result = json.loads(raw)

        assert result["returncode"] == 0
        assert result["success"] is False
        assert "missing command" in result["error"]
        assert "/tmp/tools/build-tool: not found" in result["error"]
    finally:
        db.close()


def test_shell_exec_reports_empty_which_probe_as_missing_executable(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {"command": "which definitely-missing-nipux-test-command", "timeout_seconds": 5},
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert result["returncode"] == 1
        assert result["error"] == "command probe found no executable: definitely-missing-nipux-test-command"
    finally:
        db.close()


def test_shell_exec_flags_empty_successful_probe_as_no_observation(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {"command": "find /tmp/definitely-missing-nipux-test-path -maxdepth 1 2>/dev/null || true", "timeout_seconds": 5},
            ctx,
        )
        result = json.loads(raw)

        assert result["returncode"] == 0
        assert result["success"] is False
        assert "produced no output" in result["error"]
        assert "filesystem probe" in result["error"]
    finally:
        db.close()


def test_shell_exec_flags_missing_which_probe_hidden_by_true(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {"command": "which definitely-missing-nipux-test-command || true", "timeout_seconds": 5},
            ctx,
        )
        result = json.loads(raw)

        assert result["returncode"] == 0
        assert result["success"] is False
        assert "probe found no executable" in result["error"]
    finally:
        db.close()


def test_shell_exec_flags_make_failure_hidden_by_pipe_status(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run command")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "shell_exec",
            {"command": "printf 'Makefile:6: *** Build system changed:\\n.  Stop.\\n'", "timeout_seconds": 5},
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert "build/tool failure" in result["error"]
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


def test_record_lesson_cannot_clear_measurement_obligation_with_vague_lesson(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Improve a measurable process",
            metadata={
                "pending_measurement_obligation": {
                    "source_step_no": 4,
                    "tool": "shell_exec",
                    "metric_candidates": ["42 units/s"],
                    "command": "run trial",
                }
            },
        )
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_lesson")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_lesson",
            {"lesson": "continue focused work", "category": "strategy"},
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is False
        assert result["error"] == "measurement explanation required"
        assert job["metadata"]["pending_measurement_obligation"]["source_step_no"] == 4
        assert "lessons" not in job["metadata"]
    finally:
        db.close()


def test_record_lesson_can_explain_invalid_measurement_obligation(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Improve a measurable process",
            metadata={
                "pending_measurement_obligation": {
                    "source_step_no": 4,
                    "tool": "shell_exec",
                    "metric_candidates": ["42 units/s"],
                    "command": "run trial",
                }
            },
        )
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_lesson")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_lesson",
            {
                "lesson": (
                    "The output was diagnostic only and did not contain a valid metric; "
                    "rerun the branch with a measured trial."
                ),
                "category": "mistake",
            },
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is True
        assert job["metadata"].get("pending_measurement_obligation") == {}
        assert job["metadata"]["last_measurement_obligation"]["resolution_status"] == "explained"
        assert job["metadata"]["last_measurement_obligation"]["resolution_tool"] == "record_lesson"
    finally:
        db.close()


def test_memory_graph_tools_roundtrip(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Build durable project understanding")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_memory_graph")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_memory_graph",
            {
                "nodes": [
                    {
                        "title": "Use measured checkpoints before expanding scope",
                        "kind": "strategy",
                        "status": "active",
                        "summary": "Convert branch outcomes into evidence-backed decisions before opening more work.",
                        "salience": 0.9,
                        "tags": ["progress", "validation"],
                        "evidence_refs": ["art_123"],
                    },
                    {
                        "title": "Open question: missing evaluator",
                        "kind": "question",
                        "status": "open",
                        "summary": "The job needs a concrete validation signal for the next branch.",
                    },
                ],
                "edges": [
                    {
                        "from_key": "Use measured checkpoints before expanding scope",
                        "to_key": "Open question: missing evaluator",
                        "relation": "raises",
                    }
                ],
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is True
        assert result["added_nodes"] == 2
        assert result["added_edges"] == 1
        job = db.get_job(job_id)
        graph = job["metadata"]["memory_graph"]
        assert len(graph["nodes"]) == 2
        assert graph["nodes"][0]["kind"] == "strategy"
        assert graph["nodes"][0]["evidence_refs"] == ["art_123"]
        assert db.list_events(job_id=job_id, event_types=["memory_node"])[0]["title"] == "memory graph"

        search_raw = DEFAULT_REGISTRY.handle("search_memory_graph", {"query": "evaluator"}, ctx)
        search = json.loads(search_raw)
        assert search["success"] is True
        assert search["nodes"][0]["title"] == "Open question: missing evaluator"
        assert search["edges"][0]["relation"] == "raises"
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


def test_record_source_requires_assessment(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_source")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle("record_source", {"source": "https://example.com"}, ctx)
        result = json.loads(raw)

        assert result["success"] is False
        assert result["error"] == "source assessment is required"
        assert db.get_job(job_id)["metadata"].get("source_ledger") is None
    finally:
        db.close()


def test_record_source_does_not_accept_type_without_assessment(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_source")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_source",
            {"source": "https://example.com", "source_type": "web_source"},
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert result["error"] == "source assessment is required"
    finally:
        db.close()


def test_record_findings_reports_unchanged_duplicates_without_agent_update_noise(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_findings")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)
        args = {
            "findings": [
                {
                    "name": "Reusable finding",
                    "source_url": "https://example-source.com/finding",
                    "reason": "Evidence-backed result",
                    "score": 0.75,
                }
            ]
        }

        first = json.loads(DEFAULT_REGISTRY.handle("record_findings", args, ctx))
        agent_events_after_first = len(db.list_events(job_id=job_id, event_types=["agent_message"]))
        repeated = json.loads(DEFAULT_REGISTRY.handle("record_findings", args, ctx))
        agent_events_after_repeat = len(db.list_events(job_id=job_id, event_types=["agent_message"]))

        assert first["added"] == 1
        assert first["updated"] == 0
        assert first["unchanged"] == 0
        assert repeated["added"] == 0
        assert repeated["updated"] == 0
        assert repeated["unchanged"] == 1
        assert agent_events_after_repeat == agent_events_after_first
    finally:
        db.close()


def test_record_findings_requires_evidence_anchor(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_findings")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_findings",
            {"findings": [{"name": "Unsupported label", "category": "candidate"}]},
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert result["error"] == "no valid finding with name/title and evidence was provided"
        assert result["rejected"] == [{"name": "Unsupported label", "reason": "missing_evidence"}]
        assert db.get_job(job_id)["metadata"].get("finding_ledger") is None
    finally:
        db.close()


def test_record_findings_reports_rejected_unevidenced_items_in_mixed_batch(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_findings")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_findings",
            {
                "findings": [
                    {"name": "Unsupported label"},
                    {"name": "Evidence-backed result", "metadata": {"source_url": "file:///tmp/evidence.txt"}},
                ]
            },
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is True
        assert result["added"] == 1
        assert result["rejected"] == [{"name": "Unsupported label", "reason": "missing_evidence"}]
        assert job["metadata"]["finding_ledger"][0]["name"] == "Evidence-backed result"
        assert job["metadata"]["last_agent_update"]["metadata"]["rejected"] == 1
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
        task = job["metadata"]["task_queue"][0]
        assert task["title"] == "Explore primary sources"
        assert task["priority"] == 5
        assert task["output_contract"] == "research"
        assert task["acceptance_criteria"]
        assert task["evidence_needed"]
        assert task["stall_behavior"]
        assert task["metadata"]["contract_inferred_fields"] == [
            "acceptance_criteria",
            "evidence_needed",
            "output_contract",
            "stall_behavior",
        ]
        assert job["metadata"]["last_agent_update"]["category"] == "plan"
    finally:
        db.close()


def test_record_tasks_dedupes_semantic_task_under_backlog_pressure(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Keep a long-running job focused",
            metadata={
                "task_queue": [
                    {
                        "title": "Validate model files and run baseline benchmark",
                        "status": "open",
                        "priority": 5,
                        "goal": "Get a measured baseline.",
                    },
                    *[
                        {"title": f"Done branch {index}", "status": "done", "priority": 0}
                        for index in range(81)
                    ],
                ]
            },
        )
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [
                    {
                        "title": "Validate candidate model files and run baseline benchmark",
                        "status": "active",
                        "priority": 10,
                        "goal": "Use the existing validation branch for the first measured run.",
                    }
                ]
            },
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)
        task_queue = job["metadata"]["task_queue"]

        assert result["success"] is True
        assert result["added"] == 0
        assert result["updated"] == 1
        assert len(task_queue) == 82
        task = task_queue[0]
        assert task["title"] == "Validate model files and run baseline benchmark"
        assert task["status"] == "active"
        assert task["metadata"]["original_title"] == "Validate candidate model files and run baseline benchmark"
        assert task["metadata"]["matched_existing_task"]["title"] == "Validate model files and run baseline benchmark"
    finally:
        db.close()


def test_record_tasks_reports_unchanged_duplicates_without_agent_update_noise(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)
        args = {
            "tasks": [
                {
                    "title": "Explore primary sources",
                    "status": "open",
                    "priority": 5,
                    "goal": "Find artifact-backed evidence",
                }
            ]
        }

        first = json.loads(DEFAULT_REGISTRY.handle("record_tasks", args, ctx))
        agent_events_after_first = len(db.list_events(job_id=job_id, event_types=["agent_message"]))
        db.update_job_metadata(
            job_id,
            {"pending_measurement_obligation": {"source_step_no": 1, "metric_candidates": ["score"]}},
        )
        repeated = json.loads(DEFAULT_REGISTRY.handle("record_tasks", args, ctx))
        agent_events_after_repeat = len(db.list_events(job_id=job_id, event_types=["agent_message"]))

        assert first["added"] == 1
        assert first["updated"] == 0
        assert first["unchanged"] == 0
        assert repeated["added"] == 0
        assert repeated["updated"] == 0
        assert repeated["unchanged"] == 1
        assert agent_events_after_repeat == agent_events_after_first
        assert db.get_job(job_id)["metadata"]["pending_measurement_obligation"]["source_step_no"] == 1
    finally:
        db.close()


def test_record_tasks_cannot_defer_measurement_with_unrelated_task(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Improve measurable process",
            metadata={
                "pending_measurement_obligation": {
                    "source_step_no": 8,
                    "tool": "shell_exec",
                    "metric_candidates": ["42 units/s"],
                }
            },
        )
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {"tasks": [{"title": "Read more background sources", "status": "open", "output_contract": "research"}]},
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is False
        assert result["error"] == "measurement task required"
        assert job["metadata"]["pending_measurement_obligation"]["source_step_no"] == 8
        assert "task_queue" not in job["metadata"]
    finally:
        db.close()


def test_record_tasks_can_defer_measurement_with_explicit_measurement_task(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Improve measurable process",
            metadata={
                "pending_measurement_obligation": {
                    "source_step_no": 8,
                    "tool": "shell_exec",
                    "metric_candidates": ["42 units/s"],
                }
            },
        )
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Rerun the branch and record the missing measurement",
                    "status": "open",
                    "output_contract": "experiment",
                    "acceptance_criteria": "valid metric recorded",
                    "evidence_needed": "measured command output",
                    "stall_behavior": "record blocker if measurement cannot be obtained",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)

        assert result["success"] is True
        assert result["added"] == 1
        assert job["metadata"].get("pending_measurement_obligation") == {}
        assert job["metadata"]["last_measurement_obligation"]["resolution_status"] == "deferred"
        assert job["metadata"]["last_measurement_obligation"]["resolution_tool"] == "record_tasks"
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


def test_record_milestone_validation_requires_evidence_for_passed_status(tmp_path):
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
                "validation_status": "passed",
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert result["error"] == "passed milestone validation requires evidence or result"
        assert db.get_job(job_id)["metadata"].get("roadmap") is None
    finally:
        db.close()


def test_record_milestone_validation_allows_passed_status_with_metadata_evidence(tmp_path):
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
                "validation_status": "passed",
                "metadata": {"artifact_id": "art_123"},
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is True
        assert result["validation"]["validation_status"] == "passed"
    finally:
        db.close()


def test_record_milestone_validation_requires_gap_for_failed_or_blocked_status(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Validate broad work")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_milestone_validation")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        failed = json.loads(DEFAULT_REGISTRY.handle(
            "record_milestone_validation",
            {
                "milestone": "Foundation",
                "validation_status": "failed",
            },
            ctx,
        ))
        blocked = json.loads(DEFAULT_REGISTRY.handle(
            "record_milestone_validation",
            {
                "milestone": "Foundation",
                "validation_status": "blocked",
            },
            ctx,
        ))

        assert failed["success"] is False
        assert failed["error"] == "failed milestone validation requires a gap, issue, evidence, next_action, or follow-up task"
        assert blocked["success"] is False
        assert blocked["error"] == "blocked milestone validation requires a gap, issue, evidence, next_action, or follow-up task"
        assert db.get_job(job_id)["metadata"].get("roadmap") is None
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


def test_record_experiment_synthesizes_missing_title(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "status": "planned",
                "metric_name": "download_progress_bytes",
                "result": "download incomplete",
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is True
        assert result["experiment"]["title"] == "download_progress_bytes"
    finally:
        db.close()


def test_record_experiment_requires_next_action_for_closed_trials(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "blocked attempt",
                "status": "blocked",
                "metric_name": "score",
                "result": "no valid measurement",
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert result["error"] == "next_action is required for measured, failed, blocked, or skipped experiments"
        assert db.get_job(job_id)["metadata"].get("experiment_ledger") is None
    finally:
        db.close()


def test_record_experiment_requires_context_for_closed_non_measured_trials(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "blocked attempt",
                "status": "blocked",
                "metric_name": "score",
                "next_action": "try a different branch",
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert result["error"] == "blocked experiments require result, evidence, config, or metadata"
        assert db.get_job(job_id)["metadata"].get("experiment_ledger") is None
    finally:
        db.close()


def test_record_experiment_accepts_blocked_trial_with_context(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "blocked attempt",
                "status": "blocked",
                "metric_name": "score",
                "result": "required input was unavailable",
                "next_action": "try a different branch",
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is True
        assert result["experiment"]["status"] == "blocked"
        assert result["experiment"]["result"] == "required input was unavailable"
    finally:
        db.close()


def test_record_experiment_requires_metric_for_measured_trials(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        missing_value = json.loads(DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "invalid measurement",
                "status": "measured",
                "metric_name": "score",
                "result": "looked better but no numeric metric",
                "next_action": "run a real measurement",
            },
            ctx,
        ))
        missing_name = json.loads(DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "invalid measurement",
                "status": "measured",
                "metric_value": 2.7,
                "result": "numeric result with no metric name",
                "next_action": "label the metric and retry",
            },
            ctx,
        ))

        assert missing_value["success"] is False
        assert missing_value["error"] == "measured experiments require metric_name and numeric metric_value"
        assert missing_name["success"] is False
        assert missing_name["error"] == "measured experiments require metric_name and numeric metric_value"
        assert db.get_job(job_id)["metadata"].get("experiment_ledger") is None
    finally:
        db.close()


def test_record_experiment_repairs_metric_from_pending_measurement(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Improve a measurable process",
            metadata={
                "pending_measurement_obligation": {
                    "source_step_no": 12,
                    "tool": "shell_exec",
                    "summary": "benchmark completed",
                    "metric_candidates": [
                        "pp32 4.00 ± 0.06 t/s",
                        "tg128 2.86 ± 0.04 t/s",
                    ],
                    "command": "run benchmark",
                },
            },
        )
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "benchmark result",
                "status": "measured",
                "result": "benchmark produced throughput numbers",
                "next_action": "try the next measured branch",
            },
            ctx,
        )
        result = json.loads(raw)
        job = db.get_job(job_id)
        experiment = result["experiment"]

        assert result["success"] is True
        assert experiment["metric_name"] == "tg128"
        assert experiment["metric_value"] == 2.86
        assert experiment["metric_unit"] == "t/s"
        assert experiment["metadata"]["auto_metric_from_pending_measurement"] is True
        assert experiment["metadata"]["source_step_no"] == 12
        assert job["metadata"]["pending_measurement_obligation"] == {}
        assert job["metadata"]["last_measurement_obligation"]["resolution_status"] == "recorded"
    finally:
        db.close()


def test_record_experiment_accepts_numeric_metric_strings(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_experiment",
            {
                "title": "string metric",
                "status": "measured",
                "metric_name": "score",
                "metric_value": "2.7",
                "metric_unit": "units",
                "result": "measured from output",
                "next_action": "try the next branch",
            },
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is True
        assert result["experiment"]["metric_value"] == 2.7
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


def test_acknowledge_operator_context_requires_active_context(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Run without operator corrections")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="acknowledge_operator_context")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "acknowledge_operator_context",
            {"summary": "ordinary progress note"},
            ctx,
        )
        result = json.loads(raw)

        assert result["success"] is False
        assert result["recoverable"] is True
        assert result["error"] == "no active operator context to acknowledge"
        assert "report_update" in result["guidance"]
        assert "last_operator_context_ack" not in db.get_job(job_id)["metadata"]
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


def test_record_tasks_promotes_output_contract_from_metadata(tmp_path):
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
                    "title": "Validate concrete candidate",
                    "status": "open",
                    "metadata": {"output_contract": "action", "source": "planner"},
                    "acceptance_criteria": "candidate is tested",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert result["success"] is True
        assert task["output_contract"] == "action"
        assert task["metadata"]["source"] == "planner"
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


def test_record_tasks_downgrades_done_research_without_durable_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research a topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Synthesize source evidence",
                    "status": "done",
                    "output_contract": "research",
                    "result": "Found useful background.",
                }]
            },
            ctx,
        )
        result = json.loads(raw)
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert result["success"] is True
        assert task["status"] == "active"
        assert task["metadata"]["completion_validation"] == "missing_research_evidence"
        assert task["metadata"]["claimed_result"] == "Found useful background."
    finally:
        db.close()


def test_record_tasks_allows_done_research_after_source_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research a topic")
        run_id = db.start_run(job_id, model="fake")
        source_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_source")
        db.finish_step(source_step, status="completed", summary="source recorded", output_data={"success": True})
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Synthesize source evidence",
                    "status": "done",
                    "output_contract": "research",
                    "result": "Source ledger records the useful branch.",
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


def test_record_tasks_allows_done_research_with_metadata_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research a topic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Synthesize source evidence",
                    "status": "done",
                    "output_contract": "research",
                    "metadata": {"source_url": "https://example.com/source"},
                }]
            },
            ctx,
        )
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert json.loads(raw)["success"] is True
        assert task["status"] == "done"
        assert "completion_validation" not in task.get("metadata", {})
    finally:
        db.close()


def test_record_tasks_downgrades_done_experiment_without_measurement_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Run comparison",
                    "status": "done",
                    "output_contract": "experiment",
                    "result": "The comparison improved.",
                }]
            },
            ctx,
        )
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert json.loads(raw)["success"] is True
        assert task["status"] == "active"
        assert task["metadata"]["completion_validation"] == "missing_experiment_evidence"
    finally:
        db.close()


def test_record_tasks_allows_done_experiment_after_measurement_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process")
        run_id = db.start_run(job_id, model="fake")
        experiment_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_experiment")
        db.finish_step(experiment_step, status="completed", summary="experiment measured", output_data={"success": True})
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Run comparison",
                    "status": "done",
                    "output_contract": "experiment",
                    "result": "Experiment ledger records the measured comparison.",
                }]
            },
            ctx,
        )
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert json.loads(raw)["success"] is True
        assert task["status"] == "done"
        assert "completion_validation" not in task.get("metadata", {})
    finally:
        db.close()


def test_record_tasks_downgrades_done_action_after_read_only_shell(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Change a local workspace")
        run_id = db.start_run(job_id, model="fake")
        shell_step = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="shell_exec",
            input_data={"arguments": {"command": "ls -la"}},
        )
        db.finish_step(shell_step, status="completed", summary="shell_exec rc=0")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Apply change",
                    "status": "done",
                    "output_contract": "action",
                    "result": "Inspected the workspace.",
                }]
            },
            ctx,
        )
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert json.loads(raw)["success"] is True
        assert task["status"] == "active"
        assert task["metadata"]["completion_validation"] == "missing_action_evidence"
    finally:
        db.close()


def test_record_tasks_allows_done_action_after_action_shell(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Change a local workspace")
        run_id = db.start_run(job_id, model="fake")
        shell_step = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="shell_exec",
            input_data={"arguments": {"command": "python run_branch.py"}},
        )
        db.finish_step(shell_step, status="completed", summary="shell_exec rc=0")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Apply change",
                    "status": "done",
                    "output_contract": "action",
                    "result": "Ran the action branch.",
                }]
            },
            ctx,
        )
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert json.loads(raw)["success"] is True
        assert task["status"] == "done"
        assert "completion_validation" not in task.get("metadata", {})
    finally:
        db.close()


def test_record_tasks_downgrades_done_monitor_without_defer_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Monitor long-running work")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Wait and check later",
                    "status": "done",
                    "output_contract": "monitor",
                    "result": "Will check later.",
                }]
            },
            ctx,
        )
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert json.loads(raw)["success"] is True
        assert task["status"] == "active"
        assert task["metadata"]["completion_validation"] == "missing_monitor_evidence"
    finally:
        db.close()


def test_record_tasks_allows_done_monitor_after_defer_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Monitor long-running work")
        run_id = db.start_run(job_id, model="fake")
        defer_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="defer_job")
        db.finish_step(defer_step, status="completed", summary="deferred")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_tasks")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id, run_id=run_id, step_id=step_id)

        raw = DEFAULT_REGISTRY.handle(
            "record_tasks",
            {
                "tasks": [{
                    "title": "Wait and check later",
                    "status": "done",
                    "output_contract": "monitor",
                    "result": "A monitor/defer branch is scheduled.",
                }]
            },
            ctx,
        )
        task = db.get_job(job_id)["metadata"]["task_queue"][0]

        assert json.loads(raw)["success"] is True
        assert task["status"] == "done"
        assert "completion_validation" not in task.get("metadata", {})
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
