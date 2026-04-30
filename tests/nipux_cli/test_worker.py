import json

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, RuntimeConfig
from nipux_cli.db import AgentDB
from nipux_cli.llm import LLMResponse, ScriptedLLM, ToolCall
from nipux_cli.worker import build_messages, run_one_step


class SnapshotRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del name, args, ctx
        return json.dumps({"success": True, "data": {"snapshot": "short snapshot"}})


class SuccessRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del ctx
        return json.dumps({"success": True, "tool": name, "args": args, "results": []})


class MeasuredShellRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del args, ctx
        if name == "shell_exec":
            return json.dumps({"success": True, "command": "run test", "returncode": 0, "stdout": "score 2.7 units/s", "stderr": ""})
        return json.dumps({"success": True, "results": []})


class DiagnosticShellRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del args, ctx
        if name == "shell_exec":
            return json.dumps({
                "success": True,
                "command": "df -h && nproc && free -h",
                "returncode": 0,
                "stdout": "Filesystem Size Used Avail Use% Mounted on\\n/dev/root 233G 198G 23G 90% /\\nCPU COUNT 24\\nRAM 93Gi",
                "stderr": "",
            })
        return json.dumps({"success": True})


class SourceCodeShellRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del args, ctx
        if name == "shell_exec":
            return json.dumps({
                "success": True,
                "command": "git show HEAD:nipux_cli/cli.py",
                "returncode": 0,
                "stdout": 'for index, task in enumerate(plan["tasks"], start=1):\n    rate(plan["tasks"], start=1)\n',
                "stderr": "",
            })
        return json.dumps({"success": True})


class LargeShellEvidenceRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del args, ctx
        if name == "shell_exec":
            return json.dumps({
                "success": True,
                "command": "find . -type f",
                "returncode": 0,
                "stdout": "\n".join(f"./file_{index}.py" for index in range(200)),
                "stderr": "",
            })
        return json.dumps({"success": True})


class ExtractRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del args, ctx
        if name == "web_extract":
            return json.dumps({
                "success": True,
                "pages": [
                    {"url": "https://source.example/a", "text": "useful source text " * 250},
                    {"url": "https://source.example/b", "error": "timeout"},
                ],
            })
        return json.dumps({"success": True})


class CapturingLLM:
    def __init__(self, response):
        self.response = response
        self.messages = None

    def next_action(self, *, messages, tools):
        del tools
        self.messages = messages
        return self.response


class ExplodingLLM:
    def next_action(self, *, messages, tools):
        del messages, tools
        raise AssertionError("LLM should not be called")


class AntiBotBrowserRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del args, ctx
        if name == "browser_snapshot":
            return json.dumps({
                "success": True,
                "data": {
                    "origin": "https://source.example/search",
                    "snapshot": 'Iframe "Security CAPTCHA" You have been blocked. You are browsing and clicking at a speed much faster than expected.',
                },
            })
        return json.dumps({"success": True})


def test_run_one_step_executes_scripted_tool_call(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Find 10 durable research findings", title="research", kind="generic")
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "first finding",
                        "summary": "smoke finding",
                        "content": "Acme Design, https://example.com",
                    },
                )
            ])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        assert result.tool_name == "write_artifact"
        artifacts = db.list_artifacts(job_id)
        assert artifacts[0]["title"] == "first finding"
        steps = db.list_steps(job_id=job_id)
        assert steps[0]["tool_name"] == "write_artifact"
        assert steps[0]["status"] == "completed"
        memory = db.list_memory(job_id)
        assert memory[0]["key"] == "rolling_state"
        assert artifacts[0]["id"] in memory[0]["artifact_refs"]
    finally:
        db.close()


def test_run_one_step_executes_tool_call_batch_in_order(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Build a durable report", title="batch", kind="generic")
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "evidence checkpoint",
                        "summary": "first useful output",
                        "content": "The worker saved evidence before updating the task queue.",
                    },
                ),
                ToolCall(
                    name="record_tasks",
                    arguments={
                        "tasks": [
                            {
                                "title": "Review saved output",
                                "status": "open",
                                "priority": 5,
                                "output_contract": "report",
                                "acceptance_criteria": "Saved evidence has been inspected and summarized.",
                                "evidence_needed": "Artifact reference and concrete next action.",
                                "stall_behavior": "Record a lesson and pivot if the artifact is not useful.",
                            }
                        ]
                    },
                ),
            ])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        assert result.tool_name == "record_tasks"
        steps = db.list_steps(job_id=job_id)
        assert [step["tool_name"] for step in steps] == ["write_artifact", "record_tasks"]
        assert [step["status"] for step in steps] == ["completed", "completed"]
        artifacts = db.list_artifacts(job_id)
        assert artifacts[0]["title"] == "evidence checkpoint"
        job = db.get_job(job_id)
        tasks = job["metadata"]["task_queue"]
        assert any(task["title"] == "Review saved output" and task["output_contract"] == "report" for task in tasks)
        run = db.list_runs(job_id, limit=1)[0]
        assert run["status"] == "completed"
    finally:
        db.close()


def test_write_artifact_reconciles_matching_report_task(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Write a durable report",
            title="report",
            kind="generic",
            metadata={
                "task_queue": [
                    {
                        "title": "Draft paper - Methods section",
                        "status": "open",
                        "priority": 5,
                        "output_contract": "report",
                        "acceptance_criteria": "Methods section is saved as an output.",
                    }
                ]
            },
        )
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "Paper Draft - Section 3: Methods",
                        "summary": "Methods section for the report",
                        "content": "This methods section explains the approach and evidence.",
                    },
                )
            ])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        job = db.get_job(job_id)
        task = job["metadata"]["task_queue"][0]
        assert task["status"] == "done"
        assert task["metadata"]["auto_reconciled_from_artifact"]
        assert "Saved output" in task["result"]
    finally:
        db.close()


def test_run_one_step_blocks_artifact_churn_until_progress_accounting(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep a durable progress ledger", title="ledger", kind="generic")
        for index in range(3):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="write_artifact",
                input_data={"arguments": {"title": f"Output {index}", "content": "notes"}},
            )
            db.finish_step(
                step_id,
                status="completed",
                summary=f"write_artifact saved art_{index}",
                output_data={"success": True, "artifact_id": f"art_{index}"},
            )
            db.finish_run(run_id, "completed")

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="write_artifact", arguments={"title": "Another output", "content": "more notes"})
                ])
            ]),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "progress accounting required"
        allowed = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_tasks",
                        arguments={"tasks": [{"title": "Review saved outputs", "status": "open", "priority": 2}]},
                    )
                ])
            ]),
        )
        assert allowed.status == "completed"
        assert allowed.tool_name == "record_tasks"
    finally:
        db.close()


def test_run_one_step_blocks_similar_artifact_search(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Review saved outputs", title="artifact-search", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="search_artifacts",
            input_data={"arguments": {"query": "distillation agentic paper evidence", "limit": 20}},
        )
        db.finish_step(
            step_id,
            status="completed",
            summary="search_artifacts returned 0 results",
            output_data={"success": True, "results": []},
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="search_artifacts", arguments={"query": "paper evidence for agentic distillation", "limit": 20})
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "similar artifact search blocked"
        assert result.result["blocked_tool"] == "search_artifacts"
    finally:
        db.close()


def test_run_one_step_blocks_artifact_review_when_tasks_are_exhausted(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Review saved outputs",
            title="review-exhausted",
            kind="generic",
            metadata={"task_queue": [{"title": "Review first output", "status": "done", "priority": 5}]},
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="search_artifacts", arguments={"query": "paper evidence"})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "task branch required before more work"
        assert result.result["blocked_tool"] == "search_artifacts"
    finally:
        db.close()


def test_run_one_step_recovers_repeated_guard_blocks_without_llm(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover repeated blocked work", title="guard", kind="generic")
        for index, tool_name in enumerate(["search_artifacts", "shell_exec", "read_artifact"], start=1):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name=tool_name,
                input_data={"arguments": {"query": f"blocked {index}"}},
            )
            db.finish_step(
                step_id,
                status="blocked",
                summary=f"blocked {tool_name}; progress ledger update required",
                output_data={"success": True, "recoverable": True, "error": "progress ledger update required"},
            )
            db.finish_run(run_id, "completed")

        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.status == "completed"
        assert result.tool_name == "guard_recovery"
        assert result.result["guard_recovery"]["error"] == "progress ledger update required"
        job = db.get_job(job_id)
        assert any(task["title"] == "Resolve guard: progress ledger update required" for task in job["metadata"]["task_queue"])
        assert any("Repeated guard block" in lesson["lesson"] for lesson in job["metadata"]["lessons"])
    finally:
        db.close()


def test_guard_recovery_does_not_repeat_after_recovery_step(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover repeated blocked work once", title="guard-once", kind="generic")
        for index in range(3):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="search_artifacts",
                input_data={"arguments": {"query": f"blocked {index}"}},
            )
            db.finish_step(
                step_id,
                status="blocked",
                summary="blocked search_artifacts; progress ledger update required",
                output_data={"success": True, "recoverable": True, "error": "progress ledger update required"},
            )
            db.finish_run(run_id, "completed")

        first = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())
        assert first.tool_name == "guard_recovery"

        second = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_lesson", arguments={"lesson": "Recovered guard and chose a new branch", "category": "strategy"})
                ])
            ]),
        )

        assert second.status == "completed"
        assert second.tool_name == "record_lesson"
        assert [step["tool_name"] for step in db.list_steps(job_id=job_id)[-2:]] == ["guard_recovery", "record_lesson"]
    finally:
        db.close()


def test_web_extract_auto_records_source_quality(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Track source quality", title="sources", kind="generic")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_extract", arguments={"urls": ["https://source.example/a"]})])
            ]),
            registry=ExtractRegistry(),
        )

        assert result.status == "completed"
        sources = db.get_job(job_id)["metadata"]["source_ledger"]
        assert {source["source"] for source in sources} == {"https://source.example/a", "https://source.example/b"}
        useful = next(source for source in sources if source["source"] == "https://source.example/a")
        failed = next(source for source in sources if source["source"] == "https://source.example/b")
        assert useful["usefulness_score"] >= 0.55
        assert failed["fail_count"] == 1
    finally:
        db.close()


def test_worker_cannot_mark_job_completed_by_default(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep improving forever", title="perpetual", kind="generic")
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="update_job_state",
                    arguments={"status": "completed", "note": "best result saved"},
                )
            ])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)
        job = db.get_job(job_id)

        assert result.status == "completed"
        assert result.result["kept_running"] is True
        assert job["status"] == "running"
        assert job["metadata"]["agent_updates"][-1]["metadata"]["requested_status"] == "completed"
    finally:
        db.close()


def test_run_one_step_claims_one_steering_message_per_turn(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Find durable research findings", title="research", kind="generic")
        db.append_operator_message(job_id, "first instruction", source="chat")
        db.append_operator_message(job_id, "second instruction", source="chat")
        llm = CapturingLLM(LLMResponse(content="No tool this turn."))

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        prompt = llm.messages[-1]["content"]
        job = db.get_job(job_id)
        events = db.list_timeline_events(job_id, limit=30)
        assert "first instruction" in prompt
        assert "second instruction" not in prompt
        assert job["metadata"]["operator_messages"][0]["claimed_at"]
        assert not job["metadata"]["operator_messages"][1].get("claimed_at")
        assert any(event["event_type"] == "loop" and event["title"] == "agent_start" for event in events)
        assert any(event["event_type"] == "loop" and event["title"] == "turn_end" for event in events)
    finally:
        db.close()


class FailingLLM:
    def next_action(self, *, messages, tools):
        del messages, tools
        raise RuntimeError("provider returned no choices")


def test_run_one_step_records_model_failures_instead_of_raising(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep running despite provider failures", title="provider")

        result = run_one_step(job_id, config=config, db=db, llm=FailingLLM())

        assert result.status == "failed"
        assert result.result["error"] == "provider returned no choices"
        steps = db.list_steps(job_id=job_id)
        assert steps[0]["kind"] == "llm"
        assert steps[0]["status"] == "failed"
        assert steps[0]["error"] == "provider returned no choices"
        assert db.list_runs(job_id)[0]["status"] == "failed"
    finally:
        db.close()


def test_prompt_includes_recent_tool_arguments_and_observations():
    job = {"title": "research", "kind": "generic", "objective": "find research"}
    steps = [{
        "step_no": 7,
        "kind": "tool",
        "status": "completed",
        "tool_name": "web_search",
        "summary": "web_search query='target model docs' returned 1 results",
        "input": {"arguments": {"query": "target model docs", "limit": 5}},
        "output": {"query": "target model docs", "results": [{"title": "Target Docs", "url": "https://example.com"}]},
    }]

    messages = build_messages(job, steps)

    content = messages[-1]["content"]
    assert "target model docs" in content
    assert "Target Docs <https://example.com>" in content
    assert "do not search the same query again" in content
    assert "shell_exec default cwd:" in content
    assert "read_artifact is only for those saved outputs" in content


def test_prompt_does_not_inject_local_ssh_alias_context(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text("Host remote-box\n  HostName 100.64.0.1\n  User operator\n", encoding="utf-8")
    job = {"title": "remote work", "kind": "generic", "objective": "benchmark remote target"}

    messages = build_messages(job, [])

    content = messages[-1]["content"]
    assert "Local CLI context:" not in content
    assert "100.64.0.1" not in content
    assert "remote-box ->" not in content


def test_prompt_includes_operator_steering_messages():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "find research",
        "metadata": {
            "operator_messages": [{
                "at": "2026-04-24T20:40:00+00:00",
                "source": "shell",
                "message": "Focus on actual strong evidence sources, not competing irrelevant sources.",
            }],
        },
    }

    messages = build_messages(job, [])

    assert "Operator context:" in messages[-1]["content"]
    assert "Focus on actual strong evidence sources" in messages[-1]["content"]


def test_prompt_keeps_claimed_operator_context_until_acknowledged(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Find durable research findings", title="research", kind="generic")
        entry = db.append_operator_message(job_id, "use the corrected target from chat", source="chat")
        claimed = db.claim_operator_messages(job_id, modes=("steer",), limit=1)
        assert claimed[0]["event_id"] == entry["event_id"]

        job = db.get_job(job_id)
        messages = build_messages(job, [], include_unclaimed_operator_messages=False)
        content = messages[-1]["content"]

        assert "Operator context:" in content
        assert "use the corrected target from chat" in content
        assert "delivered" in content

        db.acknowledge_operator_messages(job_id, message_ids=[entry["event_id"]], summary="incorporated correction")
        job = db.get_job(job_id)
        messages = build_messages(job, [], include_unclaimed_operator_messages=False)

        assert "use the corrected target from chat" not in messages[-1]["content"]
    finally:
        db.close()


def test_run_one_step_drops_conversation_only_chat_from_worker_prompt(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep improving a generic task", title="context", kind="generic")
        chat = db.append_operator_message(job_id, "hello", source="chat")
        correction = db.append_operator_message(job_id, "use the corrected target from chat", source="chat")
        llm = CapturingLLM(
            LLMResponse(tool_calls=[ToolCall(name="report_update", arguments={"message": "noted", "category": "progress"})])
        )

        run_one_step(job_id, config=config, db=db, llm=llm)

        content = llm.messages[-1]["content"]
        assert "hello" not in content
        assert "use the corrected target from chat" in content
        job = db.get_job(job_id)
        messages = {entry["event_id"]: entry for entry in job["metadata"]["operator_messages"]}
        assert messages[chat["event_id"]]["acknowledged_at"]
        assert messages[correction["event_id"]]["claimed_at"]
        assert not messages[correction["event_id"]].get("acknowledged_at")
    finally:
        db.close()


def test_build_messages_keeps_generic_context_under_budget():
    job = {
        "title": "large context",
        "kind": "generic",
        "objective": "Improve a measurable process without looping.",
        "metadata": {
            "operator_messages": [
                {"event_id": "chat", "mode": "steer", "message": "how is it going?"},
                {"event_id": "use", "mode": "steer", "message": "use the corrected target from chat"},
            ],
            "lessons": [{"category": "memory", "lesson": "lesson " + "x" * 700} for _ in range(30)],
            "task_queue": [
                {
                    "title": f"Task {index}",
                    "status": "open" if index % 3 else "done",
                    "priority": index,
                    "output_contract": "experiment",
                    "acceptance_criteria": "accept " + "x" * 500,
                    "evidence_needed": "evidence " + "x" * 500,
                    "stall_behavior": "stall " + "x" * 500,
                }
                for index in range(40)
            ],
            "finding_ledger": [{"name": f"Finding {index}", "category": "generic", "score": index} for index in range(200)],
            "source_ledger": [
                {
                    "source": f"https://source{index}.example",
                    "source_type": "web",
                    "usefulness_score": index / 100,
                    "yield_count": index % 4,
                    "fail_count": index % 3,
                    "last_outcome": "outcome " + "x" * 500,
                }
                for index in range(90)
            ],
            "experiment_ledger": [
                {
                    "title": f"Experiment {index}",
                    "status": "measured",
                    "metric_name": "score",
                    "metric_value": index,
                    "metric_unit": "units",
                    "best_observed": index in {38, 39},
                    "result": "result " + "x" * 600,
                    "next_action": "next " + "x" * 600,
                }
                for index in range(40)
            ],
            "reflections": [{"summary": "summary " + "x" * 800, "strategy": "strategy " + "x" * 800} for _ in range(20)],
        },
    }
    steps = [
        {
            "step_no": index,
            "kind": "tool",
            "status": "completed",
            "tool_name": "shell_exec",
            "summary": "summary " + "x" * 800,
            "input": {"arguments": {"command": "command " + "x" * 800}},
            "output": {"success": True, "command": "command", "returncode": 0, "stdout": "stdout " + "x" * 3000},
        }
        for index in range(30)
    ]
    memory_entries = [{"key": "rolling_state", "summary": "memory " + "x" * 20000, "artifact_refs": [f"art_{i}" for i in range(40)]}]
    timeline = [{"event_type": "tool_result", "title": "event", "body": "body " + "x" * 900} for _ in range(40)]

    messages = build_messages(job, steps, memory_entries=memory_entries, timeline_events=timeline)
    content = messages[-1]["content"]

    assert "use the corrected target from chat" in content
    assert "how is it going" not in content
    assert len(content) < 22000


def test_measurement_obligation_blocks_research_until_recorded(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process", title="measure", kind="generic")

        first = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "run test"})])]),
            registry=MeasuredShellRegistry(),
        )
        job = db.get_job(job_id)
        assert first.tool_name == "shell_exec"
        assert job["metadata"]["pending_measurement_obligation"]["metric_candidates"]

        second = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more notes"})])]),
            registry=MeasuredShellRegistry(),
        )
        assert second.status == "blocked"
        assert second.result["error"] == "measurement obligation pending"

        third = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_experiment",
                        arguments={
                            "title": "measured trial",
                            "status": "measured",
                            "metric_name": "score",
                            "metric_value": 2.7,
                            "metric_unit": "units/s",
                        },
                    )
                ])
            ]),
        )
        job = db.get_job(job_id)
        assert third.tool_name == "record_experiment"
        assert job["metadata"].get("pending_measurement_obligation") == {}
        assert job["metadata"]["experiment_ledger"][0]["metric_value"] == 2.7
    finally:
        db.close()


def test_diagnostic_shell_output_does_not_create_measurement_obligation(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process", title="measure", kind="generic")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "df -h && nproc && free -h"})])]),
            registry=DiagnosticShellRegistry(),
        )

        job = db.get_job(job_id)
        assert result.tool_name == "shell_exec"
        assert job["metadata"].get("pending_measurement_obligation") in (None, {})
    finally:
        db.close()


def test_source_code_shell_output_does_not_create_measurement_obligation(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process", title="measure", kind="generic")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "git show HEAD:nipux_cli/cli.py"})])
            ]),
            registry=SourceCodeShellRegistry(),
        )

        job = db.get_job(job_id)
        assert result.tool_name == "shell_exec"
        assert job["metadata"].get("pending_measurement_obligation") in (None, {})
    finally:
        db.close()


def test_large_shell_output_must_be_saved_before_more_shell_churn(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Audit a repository", title="audit", kind="generic")

        first = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "find . -type f"})])]),
            registry=LargeShellEvidenceRegistry(),
        )
        second = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "find . -name '*.md'"})])]),
            registry=LargeShellEvidenceRegistry(),
        )

        assert first.tool_name == "shell_exec"
        assert second.status == "blocked"
        assert second.result["error"] == "artifact required before more research"
    finally:
        db.close()


def test_stale_diagnostic_measurement_obligation_is_cleared(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Improve a measurable process",
            title="measure",
            kind="generic",
            metadata={
                "pending_measurement_obligation": {
                    "source_step_no": 1,
                    "command": "df -h && nproc && free -h",
                    "metric_candidates": ["CPU COUNT 24", "RAM 93"],
                }
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="record_lesson", arguments={"lesson": "continue", "category": "memory"})])]),
        )

        job = db.get_job(job_id)
        assert result.tool_name == "record_lesson"
        assert job["metadata"].get("pending_measurement_obligation") == {}
        assert "diagnostic context" in job["metadata"]["last_agent_update"]["message"]
    finally:
        db.close()


def test_measurable_objective_blocks_research_after_budget_but_allows_action(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Optimize a measurable process", title="measured", kind="generic")
        for index in range(19):
            run_id = db.start_run(job_id)
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="web_search" if index % 2 == 0 else "web_extract",
                input_data={"arguments": {"query": f"research branch {index}"}},
            )
            db.finish_step(step_id, status="completed", output_data={"success": True})
            db.finish_run(run_id, "completed")

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more research"})])]),
            registry=MeasuredShellRegistry(),
        )
        assert blocked.status == "blocked"
        assert blocked.result["error"] == "measured progress required"

        action = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "run test"})])]),
            registry=MeasuredShellRegistry(),
        )
        job = db.get_job(job_id)
        assert action.status == "completed"
        assert action.tool_name == "shell_exec"
        assert job["metadata"]["pending_measurement_obligation"]["metric_candidates"]
    finally:
        db.close()


def test_measurable_objective_blocks_shell_churn_without_experiment_accounting(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Optimize a measurable process", title="measured", kind="generic")
        for index in range(4):
            run_id = db.start_run(job_id)
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="shell_exec",
                input_data={"arguments": {"command": f"probe {index}"}},
            )
            db.finish_step(step_id, status="completed", output_data={"success": True, "stdout": "no metric"})
            db.finish_run(run_id, "completed")

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "probe again"})])]),
            registry=MeasuredShellRegistry(),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "measured progress required"
    finally:
        db.close()


def test_prompt_includes_durable_lessons():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "find research",
        "metadata": {
            "lessons": [{
                "category": "source_quality",
                "lesson": "Low-evidence pages are background noise, not durable findings.",
            }],
        },
    }

    messages = build_messages(job, [])

    content = messages[-1]["content"]
    assert "Lessons learned:" in content
    assert "Low-evidence pages are background noise" in content


def test_prompt_includes_finding_source_ledgers_and_reflections():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "find research",
        "metadata": {
            "finding_ledger": [{"name": "Acme Finding", "category": "example category", "location": "Toronto", "score": 0.8}],
            "task_queue": [{"title": "Explore primary sources", "status": "open", "priority": 5, "goal": "Find evidence"}],
            "source_ledger": [{"source": "https://example.com", "source_type": "web_source", "usefulness_score": 0.9, "yield_count": 3}],
            "reflections": [{"summary": "Directories are working", "strategy": "Use chambers next"}],
        },
    }

    messages = build_messages(job, [])

    content = messages[-1]["content"]
    assert "Finding ledger: 1 unique candidates." in content
    assert "Acme Finding" in content
    assert "Explore primary sources" in content
    assert "https://example.com" in content
    assert "Directories are working" in content


def test_prompt_includes_experiment_ledger_and_best_result():
    job = {
        "title": "improve process",
        "kind": "generic",
        "objective": "make a measurable process better",
        "metadata": {
            "experiment_ledger": [
                {
                    "title": "variant a",
                    "status": "measured",
                    "metric_name": "score",
                    "metric_value": 2.0,
                    "metric_unit": "units",
                    "higher_is_better": True,
                    "result": "baseline",
                    "best_observed": False,
                },
                {
                    "title": "variant b",
                    "status": "measured",
                    "metric_name": "score",
                    "metric_value": 3.5,
                    "metric_unit": "units",
                    "higher_is_better": True,
                    "result": "better",
                    "next_action": "try another independent variant",
                    "best_observed": True,
                },
            ],
        },
    }

    messages = build_messages(job, [])

    content = messages[-1]["content"]
    assert "Experiment ledger:" in content
    assert "Best observed results:" in content
    assert "variant b" in content
    assert "score=3.5units" in content


def test_prompt_marks_recent_anti_bot_browser_source():
    job = {"title": "research", "kind": "generic", "objective": "find research"}
    steps = [{
        "step_no": 8,
        "kind": "tool",
        "status": "completed",
        "tool_name": "browser_navigate",
        "summary": "browser_navigate opened Just a moment... <https://clutch.co/example>",
        "input": {"arguments": {"url": "https://clutch.co/example"}},
        "output": {
            "data": {"title": "Just a moment...", "url": "https://clutch.co/example"},
            "snapshot": "Performing security verification. Cloudflare security challenge.",
        },
    }]

    messages = build_messages(job, steps)

    assert "source_warning=cloudflare anti-bot challenge" in messages[-1]["content"]


def test_prompt_marks_recent_captcha_browser_block():
    job = {"title": "research", "kind": "generic", "objective": "find research"}
    steps = [{
        "step_no": 8,
        "kind": "tool",
        "status": "completed",
        "tool_name": "browser_snapshot",
        "summary": "browser_snapshot returned 1250 chars",
        "input": {"arguments": {"full": True}},
        "output": {
            "data": {
                "origin": "https://source.example/search",
                "snapshot": 'Iframe "Security CAPTCHA" You have been blocked. You are browsing and clicking at a speed much faster than expected.',
            },
        },
    }]

    messages = build_messages(job, steps)

    assert "source_warning=captcha/anti-bot block" in messages[-1]["content"]


def test_prompt_includes_browser_candidate_names():
    job = {"title": "research", "kind": "generic", "objective": "find research"}
    steps = [{
        "step_no": 9,
        "kind": "tool",
        "status": "completed",
        "tool_name": "browser_snapshot",
        "summary": "browser_snapshot returned 2000 chars",
        "input": {"arguments": {"full": False}},
        "output": {
            "data": {
                "snapshot": "source page",
                "refs": {
                    "e1": {"name": "Contact", "role": "link"},
                    "e2": {"name": "Drytech Interiors", "role": "link"},
                    "e3": {"name": "Flavour Chaser", "role": "link"},
                },
            },
        },
    }]

    messages = build_messages(job, steps)

    assert "Drytech Interiors (@e2)" in messages[-1]["content"]
    assert "Flavour Chaser (@e3)" in messages[-1]["content"]
    assert "Contact (@e1)" not in messages[-1]["content"]


def test_prompt_includes_candidate_names_from_table_cells():
    job = {"title": "research", "kind": "generic", "objective": "find research"}
    steps = [{
        "step_no": 10,
        "kind": "tool",
        "status": "completed",
        "tool_name": "browser_navigate",
        "summary": "browser_navigate opened list",
        "input": {"arguments": {"url": "https://example.com/list"}},
        "output": {
            "data": {"title": "list", "url": "https://example.com/list"},
            "snapshot": "table",
                "refs": {
                    "e100": {"name": "Organization Name", "role": "cell"},
                "e101": {"name": "Services", "role": "cell"},
                "e102": {
                    "name": "Custom ecommerce, SEO, digital strategy, headless commerce, Shopify/WooCommerce/Magento",
                    "role": "cell",
                },
                "e103": {"name": "4.8", "role": "cell"},
                "e104": {"name": "Major Tom", "role": "cell"},
                "e105": {"name": "Kffein", "role": "cell"},
            },
        },
    }]

    messages = build_messages(job, steps)

    content = messages[-1]["content"]
    assert "Major Tom (@e104)" in content
    assert "Kffein (@e105)" in content
    assert "Organization Name (@e100)" not in content
    assert "Custom ecommerce" not in content
    assert "4.8 (@e103)" not in content


def test_prompt_includes_recovery_candidates_after_stale_ref():
    job = {"title": "research", "kind": "generic", "objective": "find research"}
    steps = [{
        "step_no": 10,
        "kind": "tool",
        "status": "failed",
        "tool_name": "browser_click",
        "summary": "browser_click failed: Unknown ref: e102",
        "input": {"arguments": {"ref": "@e102"}},
        "error": "Unknown ref: e102",
        "output": {
            "success": False,
            "error": "Unknown ref: e102",
            "recovery_guidance": "The ref was stale or missing.",
            "recovery_snapshot": {
                "data": {
                    "refs": {
                        "e4": {"name": "Clearset Vac Truck Services", "role": "link"},
                    },
                },
            },
        },
    }]

    messages = build_messages(job, steps)

    content = messages[-1]["content"]
    assert "Unknown ref: e102" in content
    assert "Clearset Vac Truck Services (@e4)" in content


def test_run_one_step_blocks_exact_duplicate_tool_call(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    call = ToolCall(
        name="write_artifact",
        arguments={"title": "same", "content": "same content"},
    )
    try:
        job_id = db.create_job("Do not repeat exact tools", title="dedupe")
        first = run_one_step(job_id, config=config, db=db, llm=ScriptedLLM([LLMResponse(tool_calls=[call])]))
        second = run_one_step(job_id, config=config, db=db, llm=ScriptedLLM([LLMResponse(tool_calls=[call])]))

        assert first.status == "completed"
        assert second.status == "blocked"
        assert second.result["error"] == "duplicate tool call blocked"
        assert second.result["recoverable"] is True
        assert "previous_step" in second.result
    finally:
        db.close()


def test_duplicate_artifact_read_guidance_pushes_follow_up_work(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Use artifact once", title="artifact")
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        artifacts = ArtifactStore(tmp_path, db)
        stored = artifacts.write_text(job_id=job_id, run_id=run_id, step_id=step_id, title="Evidence", content="saved")
        db.finish_step(step_id, status="completed", output_data={"success": True, "artifact_id": stored.id, "path": str(stored.path)})
        db.finish_run(run_id, "completed")
        call = ToolCall(name="read_artifact", arguments={"artifact_id": stored.id})

        first = run_one_step(job_id, config=config, db=db, llm=ScriptedLLM([LLMResponse(tool_calls=[call])]))
        second = run_one_step(job_id, config=config, db=db, llm=ScriptedLLM([LLMResponse(tool_calls=[call])]))

        assert first.status == "completed"
        assert second.status == "blocked"
        assert "Do not read it again" in second.result["guidance"]
    finally:
        db.close()


def test_run_one_step_allows_repeated_browser_snapshot(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Snapshots are stateful", title="snap")
        first = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="browser_snapshot", arguments={"full": False})])]),
            registry=SnapshotRegistry(),
        )
        second = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="browser_snapshot", arguments={"full": False})])]),
            registry=SnapshotRegistry(),
        )

        assert first.status == "completed"
        assert second.status == "completed"
    finally:
        db.close()


def test_run_one_step_blocks_search_after_unpersisted_extract(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Save extracted evidence before more search", title="guard")
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_extract")
        db.finish_step(
            step_id,
            status="completed",
            output_data={"success": True, "pages": [{"url": "https://example.com", "text": "useful evidence"}]},
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more findings", "limit": 5})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "artifact required before more research"
        assert result.result["blocked_tool"] == "web_search"
        assert "auto_checkpoint" in result.result
        artifacts = db.list_artifacts(job_id)
        assert artifacts[0]["title"].startswith("Auto Evidence Checkpoint")

        next_result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "different findings", "limit": 5})])
            ]),
            registry=SuccessRegistry(),
        )
        assert next_result.status == "completed"
    finally:
        db.close()


def test_prompt_tells_model_to_save_unpersisted_evidence_before_more_research(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Save evidence before searching", title="guard")
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_extract")
        db.finish_step(
            step_id,
            status="completed",
            output_data={"success": True, "pages": [{"url": "https://example.com", "text": "useful evidence"}]},
        )
        job = db.get_job(job_id)
        steps = db.list_steps(job_id=job_id)

        messages = build_messages(job, steps)

        assert "Next-action constraint:" in messages[-1]["content"]
        assert "Your next tool call should usually be write_artifact" in messages[-1]["content"]
    finally:
        db.close()


def test_run_one_step_blocks_research_after_unpersisted_browser_snapshot(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Save browser evidence before more browsing", title="guard")
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="browser_snapshot")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "data": {"origin": "https://example.com"},
                "snapshot": "Useful finding evidence. " * 40,
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="browser_scroll", arguments={"direction": "down"})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "artifact required before more research"
        assert result.result["blocked_tool"] == "browser_scroll"
        assert "auto_checkpoint" in result.result
    finally:
        db.close()


def test_prompt_tells_model_to_open_new_branch_when_tasks_are_exhausted():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep improving",
        "metadata": {
            "task_queue": [
                {"title": "Initial branch", "status": "done", "priority": 5, "result": "Checkpoint saved"},
                {"title": "Blocked branch", "status": "blocked", "priority": 4, "result": "Source unavailable"},
            ],
        },
    }

    messages = build_messages(job, [])

    content = messages[-1]["content"]
    assert "All durable task branches are done" in content
    assert "use record_tasks to open the next concrete branch" in content


def test_run_one_step_blocks_branch_work_when_tasks_are_exhausted(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Keep improving without looping",
            title="exhausted",
            metadata={"task_queue": [{"title": "First branch", "status": "done", "priority": 5}]},
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "same broad topic", "limit": 5})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "task branch required before more work"
        assert result.result["blocked_tool"] == "web_search"
        assert result.result["recoverable"] is True
    finally:
        db.close()


def test_run_one_step_allows_record_tasks_when_tasks_are_exhausted(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Keep improving by opening branches",
            title="branch",
            metadata={"task_queue": [{"title": "First branch", "status": "done", "priority": 5}]},
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="record_tasks", arguments={
                    "tasks": [{"title": "Next branch", "status": "open", "priority": 6}]
                })])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_tasks"
        job = db.get_job(job_id)
        assert any(task["title"] == "Next branch" and task["status"] == "open" for task in job["metadata"]["task_queue"])
    finally:
        db.close()


def test_run_one_step_auto_records_anti_bot_browser_source(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid blocked browser pages", title="guard")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="browser_snapshot", arguments={"full": True})])
            ]),
            registry=AntiBotBrowserRegistry(),
        )
        job = db.get_job(job_id)
        source = job["metadata"]["source_ledger"][0]

        assert result.status == "completed"
        assert result.result["source_warning"] == "captcha/anti-bot block"
        assert source["source"] == "https://source.example/search"
        assert source["fail_count"] == 1
        assert source["usefulness_score"] == 0.02
        assert job["metadata"]["last_lesson"]["category"] == "source_quality"
    finally:
        db.close()


def test_run_one_step_blocks_misleading_artifact_after_anti_bot_snapshot(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Do not invent findings from blocked pages", title="guard")
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="browser_snapshot")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "data": {
                    "origin": "https://source.example/search",
                    "snapshot": 'Iframe "Security CAPTCHA" You have been blocked.',
                },
            },
            summary="browser_snapshot returned 1250 chars",
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "Directory finding source",
                        "summary": "Contains result listings for finding extraction",
                        "content": "This source contains reusable findings.",
                    },
                )])
            ]),
        )
        job = db.get_job(job_id)

        assert result.status == "blocked"
        assert result.result["error"] == "misleading blocked-source artifact blocked"
        assert result.result["auto_source_record"]["source"]["source"] == "https://source.example/search"
        assert db.list_artifacts(job_id) == []
        assert job["metadata"]["source_ledger"][0]["warnings"] == ["captcha/anti-bot block"]
    finally:
        db.close()


def test_run_one_step_allows_blocked_source_artifact_when_acknowledged(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Save blocked source notes", title="guard")
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="browser_snapshot")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "data": {
                    "origin": "https://source.example/search",
                    "snapshot": 'Iframe "Security CAPTCHA" You have been blocked.',
                },
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "Blocked source note",
                        "summary": "Blocked by CAPTCHA; not usable as finding evidence",
                        "content": "The page showed a CAPTCHA and no usable evidence was visible.",
                    },
                )])
            ]),
        )

        assert result.status == "completed"
        assert db.list_artifacts(job_id)[0]["title"] == "Blocked source note"
    finally:
        db.close()


def test_run_one_step_blocks_browser_loop_after_anti_bot_snapshot(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Pivot after blocked browser pages", title="guard")
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="browser_snapshot")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "data": {
                    "origin": "https://source.example/search",
                    "snapshot": 'Iframe "Security CAPTCHA" You have been blocked.',
                },
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="browser_scroll", arguments={"direction": "down"})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "anti-bot source loop blocked"
        assert result.result["auto_source_record"]["source"]["fail_count"] == 1
    finally:
        db.close()


def test_run_one_step_blocks_known_bad_browser_source_from_ledger(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid sources already scored as bad", title="guard")
        db.append_source_record(
            job_id,
            "https://blocked.example/search",
            source_type="blocked_browser_source",
            usefulness_score=0.02,
            fail_count_delta=1,
            warnings=["captcha/anti-bot block"],
            outcome="blocked; pivot",
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="browser_navigate", arguments={"url": "https://www.blocked.example/search?page=2"})])
            ]),
        )
        job = db.get_job(job_id)

        assert result.status == "blocked"
        assert result.result["error"] == "known bad source blocked"
        assert result.result["known_bad_source"]["source"] == "https://blocked.example/search"
        assert job["metadata"]["last_agent_update"]["category"] == "blocked"
    finally:
        db.close()


def test_run_one_step_blocks_known_bad_extract_source_from_ledger(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid extracting bad sources", title="guard")
        db.append_source_record(
            job_id,
            "https://lowyield.example/source",
            source_type="web_source",
            usefulness_score=0.05,
            fail_count_delta=2,
            outcome="no useful candidates",
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="web_extract",
                    arguments={"urls": ["https://lowyield.example/source?retry=1"]},
                )])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "known bad source blocked"
        assert result.result["known_bad_source"]["fail_count"] == 2
    finally:
        db.close()


def test_run_one_step_saves_unpersisted_evidence_before_known_bad_source_block(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Evidence checkpoint still wins", title="guard")
        db.append_source_record(
            job_id,
            "https://blocked.example/search",
            source_type="blocked_browser_source",
            usefulness_score=0.02,
            fail_count_delta=1,
            warnings=["captcha/anti-bot block"],
            outcome="blocked; pivot",
        )
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="browser_snapshot")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "data": {"origin": "https://useful.example"},
                "snapshot": "Useful source evidence. " * 80,
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="browser_navigate", arguments={"url": "https://blocked.example/search"})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "artifact required before more research"
        assert "auto_checkpoint" in result.result
        assert result.result["auto_checkpoint"]["artifact_id"]
    finally:
        db.close()


def test_run_one_step_blocks_search_streak(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Do not search forever", title="guard")
        for query in ("alpha findings", "beta findings", "gamma findings"):
            run_id = db.start_run(job_id)
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search", input_data={"arguments": {"query": query}})
            db.finish_step(step_id, status="completed", output_data={"success": True, "query": query, "results": []})
            db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "delta findings", "limit": 5})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "search loop blocked"
        assert result.result["recent_search_streak"] == 3
    finally:
        db.close()


def test_run_one_step_blocks_similar_search_query(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid query rewrites", title="guard")
        run_id = db.start_run(job_id)
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="web_search",
            input_data={"arguments": {"query": "target digital marketing research"}},
        )
        db.finish_step(step_id, status="completed", output_data={"success": True, "query": "target digital marketing research", "results": []})
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "target marketing digital research", "limit": 5})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "similar search query blocked"
    finally:
        db.close()


def test_run_one_step_reflects_every_fixed_interval(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Reflect over work", title="reflect")
        for index in range(12):
            run_id = db.start_run(job_id)
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search")
            db.finish_step(step_id, status="completed", summary=f"step {index}", output_data={"success": True})
            db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "should not be used"})])
            ]),
        )
        job = db.get_job(job_id)

        assert result.tool_name == "reflect"
        assert result.status == "completed"
        assert job["metadata"]["reflections"]
        assert job["metadata"]["last_agent_update"]["category"] == "plan"
        assert "Lessons learned:" in build_messages(job, db.list_steps(job_id=job_id))[-1]["content"]
    finally:
        db.close()
