import json
from pathlib import Path

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, ModelConfig, RuntimeConfig
from nipux_cli.db import AgentDB
from nipux_cli.llm import LLMResponse, LLMResponseError, ScriptedLLM, ToolCall
from nipux_cli.worker import MAX_WORKER_PROMPT_CHARS, SYSTEM_PROMPT, build_messages, run_one_step, _concrete_evidence_tokens, _render_worker_prompt


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


class FailedUrlShellRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del ctx
        if name == "shell_exec":
            return json.dumps({
                "success": False,
                "command": args.get("command"),
                "returncode": 0,
                "stdout": "401 Unauthorized",
                "stderr": "",
                "error": (
                    "command output indicates authentication or authorization failure "
                    "despite exit status 0: 401 Unauthorized"
                ),
            })
        return json.dumps({"success": True})


class HangingLLM:
    def next_action(self, *, messages, tools):
        del messages, tools
        import time

        time.sleep(5)
        return LLMResponse(tool_calls=[ToolCall(name="report_update", arguments={"message": "late"})])


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


class SearchRegistry:
    def openai_tools(self):
        return []

    def handle(self, name, args, ctx):
        del args, ctx
        if name == "web_search":
            return json.dumps({
                "success": True,
                "query": "durable progress research",
                "results": [
                    {"title": "Primary reference", "url": "https://source.example/primary"},
                    {"title": "Secondary reference", "url": "https://source.example/secondary"},
                ],
            })
        return json.dumps({"success": True})


class BrowserAndWebRegistry:
    def openai_tools(self, config=None):
        del config
        return [
            {"type": "function", "function": {"name": "browser_navigate", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}},
        ]

    def handle(self, name, args, ctx):
        del args, ctx
        return json.dumps({"success": True, "tool": name})


class CapturingLLM:
    def __init__(self, response):
        self.response = response
        self.messages = None
        self.tools = None

    def next_action(self, *, messages, tools):
        self.messages = messages
        self.tools = tools
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


def test_system_prompt_is_contract_first_not_research_first():
    assert "Use a contract-first durable cycle" in SYSTEM_PROMPT
    assert "Research is only one possible contract" in SYSTEM_PROMPT
    assert "Prefer fresh measured or directly observed evidence over stale summaries" in SYSTEM_PROMPT
    assert "available local candidate fall" in SYSTEM_PROMPT
    assert "Use this durable cycle: discover one source" not in SYSTEM_PROMPT


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


def test_run_one_step_records_estimated_usage_for_scripted_model(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Summarize progress", title="usage", kind="generic")

        run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(content="No tool this turn.")]),
        )

        usage = db.job_token_usage(job_id)
        assert usage["calls"] == 1
        assert usage["prompt_tokens"] > 0
        assert usage["completion_tokens"] > 0
        assert usage["estimated_calls"] == 1
        event = next(
            event
            for event in db.list_events(job_id=job_id, event_types=["loop"])
            if event.get("title") == "message_end"
        )
        event_usage = event["metadata"]["usage"]
        assert event_usage["prompt_chars"] > 0
        assert event_usage["context_length"] == config.model.context_length
        assert event_usage["context_fraction"] > 0
    finally:
        db.close()


def test_run_one_step_blocks_content_only_worker_turn(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep taking bounded tool actions", title="no tool", kind="generic")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(content="What should I do next?")]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "worker tool call required"
        assert "What should I do next?" in result.result["content"]
        step = db.list_steps(job_id=job_id)[0]
        assert step["kind"] == "assistant"
        assert step["status"] == "blocked"
        assert step["error"] == "worker tool call required"
        prompt = build_messages(
            db.get_job(job_id),
            db.list_steps(job_id=job_id),
            timeline_events=db.list_timeline_events(job_id, limit=30),
        )[-1]["content"]
        assert "What should I do next?" not in prompt
        assert "worker tool call required" in prompt
        job = db.get_job(job_id)
        assert job["metadata"]["last_agent_update"]["category"] == "blocked"
    finally:
        db.close()


def test_run_one_step_recovers_repeated_content_only_worker_turns(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep taking bounded tool actions", title="no tool", kind="generic")
        llm = ScriptedLLM([
            LLMResponse(content="What should I do next?"),
            LLMResponse(content="I can continue if you want."),
            LLMResponse(content="Please confirm the next step."),
        ])

        run_one_step(job_id, config=config, db=db, llm=llm)
        run_one_step(job_id, config=config, db=db, llm=llm)
        run_one_step(job_id, config=config, db=db, llm=llm)
        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.status == "completed"
        assert result.tool_name == "guard_recovery"
        assert result.result["guard_recovery"]["error"] == "worker tool call required"
        job = db.get_job(job_id)
        assert any(task["title"] == "Resolve guard: worker tool call required" for task in job["metadata"]["task_queue"])
    finally:
        db.close()


def test_run_one_step_records_context_pressure_without_spam(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path), model=ModelConfig(context_length=10_000))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep a long-running task stable", title="context pressure", kind="generic")
        llm = ScriptedLLM([
            LLMResponse(content="first", usage={"prompt_tokens": 7_000, "completion_tokens": 10, "total_tokens": 7_010}),
            LLMResponse(content="second", usage={"prompt_tokens": 7_200, "completion_tokens": 10, "total_tokens": 7_210}),
            LLMResponse(content="third", usage={"prompt_tokens": 8_600, "completion_tokens": 10, "total_tokens": 8_610}),
        ])

        run_one_step(job_id, config=config, db=db, llm=llm)
        run_one_step(job_id, config=config, db=db, llm=llm)
        run_one_step(job_id, config=config, db=db, llm=llm)

        pressure_events = [
            event
            for event in db.list_events(job_id=job_id, event_types=["agent_message"])
            if event["metadata"].get("kind") == "context_pressure"
        ]
        assert len(pressure_events) == 2
        assert "Context pressure watch" in pressure_events[0]["body"]
        assert "Context pressure high" in pressure_events[1]["body"]
        job = db.get_job(job_id)
        pressure = job["metadata"]["context_pressure"]
        assert pressure["band"] == "high"
        assert pressure["prompt_tokens"] == 8_600
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
        revision_tasks = [
            item
            for item in job["metadata"]["task_queue"]
            if item["status"] == "open" and item.get("metadata", {}).get("source") == "auto_revision_loop"
        ]
        assert len(revision_tasks) == 1
        assert revision_tasks[0]["output_contract"] == "report"
        assert revision_tasks[0]["metadata"]["revision_source_artifact_id"]
    finally:
        db.close()


def test_evidence_artifact_does_not_complete_deliverable_task(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Improve a durable report",
            title="report",
            kind="generic",
            metadata={
                "task_queue": [
                    {
                        "title": "Update report with new citations",
                        "status": "open",
                        "priority": 5,
                        "output_contract": "artifact",
                        "acceptance_criteria": "Report text is updated with citations.",
                        "evidence_needed": "Updated report draft, not just source notes.",
                    }
                ]
            },
        )
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "Evidence: citation sources",
                        "summary": "Extracted source notes for citations",
                        "content": "These notes describe sources that could later be used in the report.",
                    },
                )
            ])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        job = db.get_job(job_id)
        task = job["metadata"]["task_queue"][0]
        assert task["status"] == "open"
        assert "auto_reconciled_from_artifact" not in task.get("metadata", {})
        assert not [
            item
            for item in job["metadata"]["task_queue"]
            if item.get("metadata", {}).get("source") == "auto_revision_loop"
        ]
    finally:
        db.close()


def test_new_deliverable_supersedes_old_auto_revision_task(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Keep improving a durable report",
            title="report",
            kind="generic",
            metadata={
                "task_queue": [
                    {
                        "title": "Review and revise saved output art_old",
                        "status": "open",
                        "priority": 4,
                        "output_contract": "report",
                        "metadata": {
                            "source": "auto_revision_loop",
                            "revision_source_artifact_id": "art_old",
                        },
                    }
                ]
            },
        )
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "Report Draft Revision",
                        "summary": "Updated durable report draft",
                        "content": "This revised report draft supersedes the previous saved output.",
                    },
                )
            ])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        tasks = db.get_job(job_id)["metadata"]["task_queue"]
        old = next(task for task in tasks if task["metadata"].get("revision_source_artifact_id") == "art_old")
        new = next(task for task in tasks if task["metadata"].get("revision_source_artifact_id") != "art_old")
        assert old["status"] == "skipped"
        assert old["metadata"]["superseded_by_artifact_id"]
        assert new["status"] == "open"
        assert new["metadata"]["source"] == "auto_revision_loop"
    finally:
        db.close()


def test_audit_report_draft_counts_as_deliverable_output(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Write a durable audit report",
            title="audit report",
            kind="generic",
            metadata={
                "task_queue": [
                    {
                        "title": "Write audit report draft",
                        "status": "open",
                        "priority": 5,
                        "output_contract": "artifact",
                        "acceptance_criteria": "A report draft is saved.",
                        "evidence_needed": "Saved report draft, not only notes.",
                    }
                ]
            },
        )
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "Market Readiness Audit Report Draft",
                        "summary": "Saved audit report draft with current findings and recommendations",
                        "content": "This is the current audit report draft.",
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
    finally:
        db.close()


def test_checkpoint_artifact_does_not_complete_deliverable_task(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Compile a durable report",
            title="report",
            kind="generic",
            metadata={
                "task_queue": [
                    {
                        "title": "Compile full report",
                        "status": "open",
                        "priority": 5,
                        "output_contract": "artifact",
                        "acceptance_criteria": "Final compiled report is saved.",
                    }
                ]
            },
        )
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "Compiled report checkpoint",
                        "summary": "Current state checkpoint, not a final compiled report",
                        "content": "This checkpoint describes what still needs to be written.",
                    },
                )
            ])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        job = db.get_job(job_id)
        task = job["metadata"]["task_queue"][0]
        assert task["status"] == "open"
        assert "auto_reconciled_from_artifact" not in task.get("metadata", {})
    finally:
        db.close()


def test_evidence_artifact_can_complete_research_task(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Gather source evidence",
            title="research",
            kind="generic",
            metadata={
                "task_queue": [
                    {
                        "title": "Collect citation source evidence",
                        "status": "open",
                        "priority": 5,
                        "output_contract": "research",
                        "acceptance_criteria": "Evidence sources are saved.",
                    }
                ]
            },
        )
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="write_artifact",
                    arguments={
                        "title": "Evidence: citation sources",
                        "summary": "Extracted source evidence",
                        "content": "Citation source evidence for later report writing.",
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


def test_activity_checkpoint_streak_blocks_more_churn_until_ledger_update(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep working until durable progress appears", title="stagnation", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "activity_checkpoint_streak": 3,
                "last_checkpoint_counts": {
                    "findings": 0,
                    "sources": 0,
                    "tasks": 1,
                    "experiments": 0,
                    "lessons": 0,
                    "milestones": 0,
                },
            },
        )

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more background"})])]),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "durable progress required"

        allowed = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="record_tasks", arguments={"tasks": [{"title": "Pivot branch", "status": "open"}]})])
            ]),
        )

        assert allowed.status == "completed"
        assert allowed.tool_name == "record_tasks"
    finally:
        db.close()


def test_task_only_checkpoint_streak_blocks_new_task_sprawl(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep executing durable work", title="task-sprawl", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "task_planning_checkpoint_streak": 2,
                "task_queue": [
                    {
                        "key": "existing-branch",
                        "title": "Existing branch",
                        "status": "open",
                    }
                ],
            },
        )

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_tasks",
                        arguments={"tasks": [{"title": "Another open branch", "status": "open"}]},
                    )
                ])
            ]),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "task execution required"

        allowed = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_tasks",
                        arguments={
                            "tasks": [
                                {
                                    "title": "Existing branch",
                                    "status": "done",
                                    "result": "Executed and checkpointed.",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert allowed.status == "completed"
        assert allowed.tool_name == "record_tasks"
    finally:
        db.close()


def test_task_only_checkpoint_updates_planning_streak(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Track planning-only progress", title="task-streak", kind="generic")
        for index in range(9):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search")
            db.finish_step(step_id, status="completed", summary=f"search {index}", output_data={"success": True})
            db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_tasks", arguments={"tasks": [{"title": "First branch", "status": "open"}]})
                ])
            ]),
        )

        assert result.status == "completed"
        job = db.get_job(job_id)
        assert job["metadata"]["task_planning_checkpoint_streak"] == 1

        db.append_finding_record(job_id, name="Durable finding")
        for index in range(9):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search")
            db.finish_step(step_id, status="completed", summary=f"search reset {index}", output_data={"success": True})
            db.finish_run(run_id, "completed")
        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_tasks", arguments={"tasks": [{"title": "Second branch", "status": "open"}]})
                ])
            ]),
        )

        assert result.status == "completed"
        job = db.get_job(job_id)
        assert job["metadata"]["task_planning_checkpoint_streak"] == 0
    finally:
        db.close()


def test_task_resolution_checkpoint_resets_planning_streak(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Resolve existing durable branches", title="task-resolution", kind="generic")
        db.append_task_record(job_id, title="Existing branch", status="open", priority=5)
        db.update_job_metadata(
            job_id,
            {
                "last_checkpoint_counts": {
                    "findings": 0,
                    "sources": 0,
                    "tasks": 1,
                    "experiments": 0,
                    "lessons": 0,
                    "milestones": 0,
                },
                "last_checkpoint_at": "2026-01-01T00:00:00+00:00",
                "task_planning_checkpoint_streak": 2,
            },
        )
        for index in range(9):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search")
            db.finish_step(step_id, status="completed", summary=f"search {index}", output_data={"success": True})
            db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_tasks",
                        arguments={
                            "tasks": [
                                {
                                    "title": "Existing branch",
                                    "status": "done",
                                    "result": "Resolved using the latest evidence.",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        job = db.get_job(job_id)
        assert job["metadata"]["task_planning_checkpoint_streak"] == 0
        assert job["metadata"]["last_agent_update"]["category"] == "progress"
        assert job["metadata"]["last_agent_update"]["metadata"]["updates"]["tasks"] == 1
        assert job["metadata"]["last_agent_update"]["metadata"]["resolutions"]["tasks"] == 1
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


def test_run_one_step_recovers_repeated_evidence_grounding_blocks(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover repeated grounding failures", title="grounding-guard", kind="generic")
        for index in range(3):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="record_experiment",
                input_data={"arguments": {"title": f"Unsupported record {index}"}},
            )
            db.finish_step(
                step_id,
                status="blocked",
                summary="blocked record_experiment; evidence grounding required",
                output_data={"success": False, "recoverable": True, "error": "evidence grounding required"},
            )
            db.finish_run(run_id, "completed")

        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.status == "completed"
        assert result.tool_name == "guard_recovery"
        assert result.result["guard_recovery"]["error"] == "evidence grounding required"
    finally:
        db.close()


def test_run_one_step_recovers_repeated_known_bad_source_blocks(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid repeatedly blocked sources", title="guard")
        for index in range(3):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="web_extract",
                input_data={"arguments": {"urls": ["https://bad.example/source"]}},
            )
            db.finish_step(
                step_id,
                status="blocked",
                summary="blocked web_extract; known bad source https://bad.example/source",
                output_data={"success": False, "error": "known bad source blocked"},
            )
            db.finish_run(run_id, "completed")

        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.status == "completed"
        assert result.tool_name == "guard_recovery"
        assert result.result["guard_recovery"]["error"] == "known bad source blocked"
        job = db.get_job(job_id)
        assert any(task["title"] == "Resolve guard: known bad source blocked" for task in job["metadata"]["task_queue"])
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


def test_guard_recovery_does_not_keep_reopening_same_guard(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover repeated blocked work once", title="guard-repeat", kind="generic")
        for batch in range(2):
            for index in range(3):
                run_id = db.start_run(job_id, model="test")
                step_id = db.add_step(
                    job_id=job_id,
                    run_id=run_id,
                    kind="tool",
                    tool_name="search_artifacts",
                    input_data={"arguments": {"query": f"blocked {batch}-{index}"}},
                )
                db.finish_step(
                    step_id,
                    status="blocked",
                    summary="blocked search_artifacts; progress ledger update required",
                    output_data={"success": False, "recoverable": True, "error": "progress ledger update required"},
                )
                db.finish_run(run_id, "completed")
            result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM() if batch == 0 else ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="record_lesson", arguments={"lesson": "Use a different branch", "category": "strategy"})])
            ]))

        steps = db.list_steps(job_id=job_id)
        assert sum(1 for step in steps if step["tool_name"] == "guard_recovery") == 1
        assert result.tool_name == "record_lesson"
    finally:
        db.close()


def test_guard_recovery_reopens_same_guard_after_progress(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover repeated blocked work after progress", title="guard-progress", kind="generic")
        for index in range(3):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="search_artifacts",
                input_data={"arguments": {"query": f"blocked first {index}"}},
            )
            db.finish_step(
                step_id,
                status="blocked",
                summary="blocked search_artifacts; progress ledger update required",
                output_data={"success": False, "recoverable": True, "error": "progress ledger update required"},
            )
            db.finish_run(run_id, "completed")

        first = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())
        assert first.tool_name == "guard_recovery"

        run_id = db.start_run(job_id, model="test")
        progress_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_lesson")
        db.finish_step(progress_step, status="completed", output_data={"success": True, "lesson": "Recovered once."})
        db.finish_run(run_id, "completed")

        for index in range(3):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="read_artifact",
                input_data={"arguments": {"query": f"blocked second {index}"}},
            )
            db.finish_step(
                step_id,
                status="blocked",
                summary="blocked read_artifact; progress ledger update required",
                output_data={"success": False, "recoverable": True, "error": "progress ledger update required"},
            )
            db.finish_run(run_id, "completed")

        second = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())
        assert second.tool_name == "guard_recovery"
        assert sum(1 for step in db.list_steps(job_id=job_id) if step["tool_name"] == "guard_recovery") == 2
    finally:
        db.close()


def test_guard_recovery_accounts_pending_evidence_checkpoint(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover checkpoint accounting deadlock", title="checkpoint-recovery", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "read_at": "2026-01-01T00:00:00+00:00",
                    "evidence_step_no": 1,
                    "blocked_tool": "shell_exec",
                }
            },
        )
        for index in range(3):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="read_artifact",
                input_data={"arguments": {"artifact_id": "art_checkpoint", "retry": index}},
            )
            db.finish_step(
                step_id,
                status="blocked",
                summary="blocked read_artifact; evidence checkpoint accounting required",
                output_data={"success": False, "recoverable": True, "error": "evidence checkpoint accounting required"},
            )
            db.finish_run(run_id, "completed")

        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.tool_name == "guard_recovery"
        pending = db.get_job(job_id)["metadata"]["pending_evidence_checkpoint"]
        assert pending["resolved_at"]
        assert pending["resolved_by_tool"] == "guard_recovery"
    finally:
        db.close()


def test_guard_recovery_immediately_recovers_already_read_checkpoint_reread(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover checkpoint reread deadlock", title="checkpoint-recovery", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="read_artifact",
            input_data={"arguments": {"artifact_id": "art_checkpoint"}},
        )
        db.finish_step(
            step_id,
            status="blocked",
            summary="blocked read_artifact; evidence checkpoint accounting required",
            output_data={
                "success": False,
                "recoverable": True,
                "error": "evidence checkpoint accounting required",
                "blocked_tool": "read_artifact",
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "checkpoint_read": True,
                    "read_at": "2026-01-01T00:00:00+00:00",
                },
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.tool_name == "guard_recovery"
        assert result.result["guard_recovery"]["count"] == 1
        assert result.result["guard_recovery"]["error"] == "evidence checkpoint accounting required"
    finally:
        db.close()


def test_prompt_does_not_tell_worker_to_reread_checkpoint_after_it_was_read(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Account for checkpoint", title="checkpoint-prompt", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "read_at": "2026-01-01T00:00:00+00:00",
                    "evidence_step_no": 1,
                    "blocked_tool": "shell_exec",
                }
            },
        )

        content = build_messages(db.get_job(job_id), db.list_steps(job_id=job_id))[-1]["content"]

        assert "Do not read the checkpoint again" in content
        assert "Next either read that checkpoint artifact" not in content
    finally:
        db.close()


def test_checkpoint_reread_block_requires_accounting_not_more_reads(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Account for checkpoint", title="checkpoint-reread", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "read_at": "2026-01-01T00:00:00+00:00",
                    "evidence_step_no": 1,
                    "blocked_tool": "shell_exec",
                }
            },
        )

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="read_artifact", arguments={"artifact_id": "art_checkpoint"})])
            ]),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "evidence checkpoint accounting required"
        assert blocked.result["checkpoint_already_read"] is True
        assert blocked.result["required_next_action"] == "durable_checkpoint_accounting"
        assert "Do not read it again" in blocked.result["guidance"]

        recovery = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert recovery.tool_name == "guard_recovery"
        task = recovery.result["task"]
        assert task["metadata"]["resolves_evidence_checkpoint"] is True
        assert "Do not read the same checkpoint again" in task["acceptance_criteria"]
    finally:
        db.close()


def test_already_read_checkpoint_branch_block_recovers_immediately(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover checkpoint branch deadlock", title="checkpoint-branch", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "read_at": "2026-01-01T00:00:00+00:00",
                    "evidence_step_no": 1,
                    "blocked_tool": "shell_exec",
                }
            },
        )
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="shell_exec",
            input_data={"arguments": {"command": "echo more branch work"}},
        )
        db.finish_step(
            step_id,
            status="blocked",
            summary="blocked shell_exec; evidence checkpoint accounting required",
            output_data={
                "success": False,
                "recoverable": True,
                "error": "evidence checkpoint accounting required",
                "checkpoint_already_read": True,
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "checkpoint_read": True,
                },
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.tool_name == "guard_recovery"
        assert result.result["guard_recovery"]["count"] == 1
        assert result.result["task"]["metadata"]["resolves_evidence_checkpoint"] is True
        pending = db.get_job(job_id)["metadata"]["pending_evidence_checkpoint"]
        assert pending["resolved_by_tool"] == "guard_recovery"
    finally:
        db.close()


def test_evidence_grounding_ignores_format_protocol_tokens():
    tokens = _concrete_evidence_tokens(
        "Parsed JSON from HTTPS REST API URL and saved HTML/YAML/XML CDN SHA256 GGUF excerpts for Model-7B step_123_shell_output. "
        "Download investigation parsed direct API results."
    )

    assert "JSON" not in tokens
    assert "HTTPS" not in tokens
    assert "REST" not in tokens
    assert "API" not in tokens
    assert "CDN" not in tokens
    assert "SHA256" not in tokens
    assert "GGUF" not in tokens
    assert "URL" not in tokens
    assert "Download" not in tokens
    assert "investigation" not in tokens
    assert "direct" not in tokens
    assert "step_123_shell_output" not in tokens
    assert "Model-7B" in tokens


def test_web_search_auto_records_source_quality(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Track search sources", title="search-sources", kind="generic")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "durable progress research"})])
            ]),
            registry=SearchRegistry(),
        )

        assert result.status == "completed"
        sources = db.get_job(job_id)["metadata"]["source_ledger"]
        assert {source["source"] for source in sources} == {
            "https://source.example/primary",
            "https://source.example/secondary",
        }
        assert all(source["source_type"] == "web_search" for source in sources)
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


def test_report_update_completion_claim_is_rewritten_as_checkpoint(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep improving forever", title="perpetual", kind="generic")
        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="report_update",
                        arguments={"message": "Job completed. Best result saved.", "category": "progress"},
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        update = db.get_job(job_id)["metadata"]["last_agent_update"]
        assert update["message"] == "Checkpoint reported; continuing work. Best result saved."
        assert update["metadata"]["rewritten_completion_claim"] is True
        assert update["metadata"]["original_message"] == "Job completed. Best result saved."
        assert update["metadata"]["follow_up_task"]
        tasks = db.get_job(job_id)["metadata"]["task_queue"]
        follow_up = next(task for task in tasks if task["key"] == update["metadata"]["follow_up_task"])
        assert follow_up["title"] == "Audit latest checkpoint against objective"
        assert follow_up["status"] == "open"
        assert follow_up["output_contract"] == "decision"
        assert follow_up["metadata"]["completion_audit_required"] is True
    finally:
        db.close()


def test_run_one_step_claims_one_message_but_keeps_all_steering_in_prompt(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Find durable research findings", title="research", kind="generic")
        db.append_operator_message(job_id, "first instruction", source="chat")
        db.append_operator_message(job_id, "second instruction", source="chat")
        llm = CapturingLLM(LLMResponse(content="No tool this turn."))

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "blocked"
        assert result.result["error"] == "worker tool call required"
        prompt = llm.messages[-1]["content"]
        job = db.get_job(job_id)
        events = db.list_timeline_events(job_id, limit=30)
        assert "first instruction" in prompt
        assert "second instruction" in prompt
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


class HardProviderFailingLLM:
    def next_action(self, *, messages, tools):
        del messages, tools
        raise LLMResponseError(
            "Key limit exceeded (total limit)",
            payload={"error": {"message": "Key limit exceeded (total limit)", "code": 403}},
        )


def test_run_one_step_records_model_failures_instead_of_raising(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep running despite provider failures", title="provider")

        result = run_one_step(job_id, config=config, db=db, llm=FailingLLM())

        assert result.status == "failed"
        assert result.result["error"] == "provider returned no choices"
        assert result.result["duration_seconds"] >= 0
        steps = db.list_steps(job_id=job_id)
        assert steps[0]["kind"] == "llm"
        assert steps[0]["status"] == "failed"
        assert steps[0]["error"] == "provider returned no choices"
        assert steps[0]["input"]["duration_seconds"] >= 0
        assert db.list_runs(job_id)[0]["status"] == "failed"
    finally:
        db.close()


def test_run_one_step_blocks_missing_tool_arguments_as_recoverable(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep running despite malformed tool calls", title="tool args")
        llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={})])])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "blocked"
        assert result.result["recoverable"] is True
        assert result.result["missing_arguments"] == ["command"]
        step = db.list_steps(job_id=job_id)[0]
        assert step["status"] == "blocked"
        assert "missing required arguments" in step["summary"]
        assert not step["error"]
    finally:
        db.close()


def test_run_one_step_blocks_placeholder_tool_arguments_as_recoverable(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep running despite placeholder tool calls", title="placeholder args")
        llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="read_artifact", arguments={"artifact_id": "..."})])])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "blocked"
        assert result.result["recoverable"] is True
        assert result.result["error"] == "missing required tool arguments"
        assert result.result["missing_arguments"] == ["artifact reference"]
        step = db.list_steps(job_id=job_id)[0]
        assert step["status"] == "blocked"
        assert "missing required arguments" in step["summary"]
    finally:
        db.close()


def test_run_one_step_blocks_truncated_optional_reference_arguments(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Resolve concrete optional references before recording", title="truncated optional")
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(
                    name="record_experiment",
                    arguments={
                        "title": "Validate artifact",
                        "evidence_artifact": "art_123...",
                        "next_action": "read the concrete artifact id",
                    },
                )
            ])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "blocked"
        assert result.result["recoverable"] is True
        assert result.result["error"] == "placeholder tool arguments"
        assert result.result["placeholder_arguments"] == ["evidence_artifact"]
        step = db.list_steps(job_id=job_id)[0]
        assert step["status"] == "blocked"
        assert "placeholder tool arguments" in step["summary"]
    finally:
        db.close()


def test_run_one_step_blocks_placeholder_shell_command_before_execution(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Resolve concrete shell inputs before execution", title="placeholder shell")
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "wget http://output/"})])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "blocked"
        assert result.result["error"] == "unresolved placeholder in shell command"
        assert result.result["placeholder"]["value"] == "http://output/"
        assert result.result["recoverable"] is True
        step = db.list_steps(job_id=job_id)[0]
        assert step["status"] == "blocked"
        assert "unresolved placeholder" in step["summary"]
    finally:
        db.close()


def test_run_one_step_times_out_stalled_model_call(tmp_path):
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(request_timeout_seconds=0.05),
    )
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep daemon moving through stalled model calls", title="provider")

        result = run_one_step(job_id, config=config, db=db, llm=HangingLLM())

        assert result.status == "failed"
        assert "model call timed out" in result.result["error"]
        assert result.result["duration_seconds"] >= 0.04
        step = db.list_steps(job_id=job_id)[0]
        assert step["kind"] == "llm"
        assert step["status"] == "failed"
        assert step["input"]["duration_seconds"] >= 0.04
    finally:
        db.close()


def test_run_one_step_defers_after_repeated_transient_model_timeouts(tmp_path):
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(request_timeout_seconds=120),
    )
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep running through provider instability", title="provider cooldown")
        for _index in range(2):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="llm", status="failed")
            db.finish_step(
                step_id,
                status="failed",
                summary="model call failed: APITimeoutError",
                output_data={"success": False, "error": "Request timed out.", "error_type": "APITimeoutError"},
                error="Request timed out.",
            )
            db.finish_run(run_id, "failed", error="Request timed out.")

        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.status == "completed"
        assert result.tool_name == "defer_job"
        assert result.result["transient_model_failure"]["count"] == 2
        job = db.get_job(job_id)
        assert job["metadata"]["defer_until"]
        assert "Transient model provider failures" in job["metadata"]["defer_reason"]
    finally:
        db.close()


def test_repeated_transient_model_cooldowns_back_off_progressively(tmp_path):
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(request_timeout_seconds=120),
    )
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep running through repeated provider instability", title="provider cooldown")
        db.update_job_metadata(job_id, {"transient_model_cooldown_streak": 2})
        for _index in range(2):
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="llm", status="failed")
            db.finish_step(
                step_id,
                status="failed",
                summary="model call failed: APITimeoutError",
                output_data={"success": False, "error": "Request timed out.", "error_type": "APITimeoutError"},
                error="Request timed out.",
            )
            db.finish_run(run_id, "failed", error="Request timed out.")

        result = run_one_step(job_id, config=config, db=db, llm=ExplodingLLM())

        assert result.status == "completed"
        assert result.tool_name == "defer_job"
        assert result.result["cooldown_streak"] == 3
        assert result.result["cooldown_seconds"] >= 700
        job = db.get_job(job_id)
        assert job["metadata"]["transient_model_cooldown_streak"] == 3
    finally:
        db.close()


def test_successful_model_response_resets_transient_model_cooldown_streak(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover after provider instability", title="provider recovered")
        db.update_job_metadata(job_id, {"transient_model_cooldown_streak": 3})
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(name="report_update", arguments={"message": "provider recovered"})])
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        assert result.tool_name == "report_update"
        job = db.get_job(job_id)
        assert job["metadata"]["transient_model_cooldown_streak"] == 0
        assert job["metadata"]["transient_model_recovered_at"]
        message_end = next(event for event in db.list_events(job_id=job_id, limit=10) if event["event_type"] == "loop" and event["title"] == "message_end")
        assert message_end["metadata"]["duration_seconds"] >= 0
    finally:
        db.close()


def test_run_one_step_pauses_job_on_hard_provider_failure(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep running when provider is configured", title="provider")

        result = run_one_step(job_id, config=config, db=db, llm=HardProviderFailingLLM())

        assert result.status == "failed"
        assert result.result["provider_action_required"] is True
        assert result.result["pause_reason"] == "llm_provider_blocked"
        job = db.get_job(job_id)
        assert job["status"] == "paused"
        assert "operator action" in job["metadata"]["last_note"]
        assert job["metadata"]["provider_blocked_at"]
        events = db.list_events(job_id=job_id, limit=10)
        assert any(event["event_type"] == "agent_message" and event["title"] == "error" for event in events)
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
    assert "shell_exec runs on the machine hosting this Nipux worker" in content
    assert str(Path.cwd()) not in content
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


def test_prompt_keeps_unclaimed_steering_but_not_followup_until_claimed(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Find durable research findings", title="research", kind="generic")
        db.append_operator_message(job_id, "use the corrected target from chat", source="chat", mode="steer")
        db.append_operator_message(job_id, "after this branch settles, write a recap", source="chat", mode="follow_up")

        job = db.get_job(job_id)
        content = build_messages(job, [], include_unclaimed_operator_messages=True)[-1]["content"]

        assert "use the corrected target from chat" in content
        assert "after this branch settles" not in content
    finally:
        db.close()


def test_prompt_includes_context_pressure_constraint():
    job = {
        "title": "context pressure",
        "kind": "generic",
        "objective": "keep a long-running job stable",
        "metadata": {
            "context_pressure": {
                "band": "high",
                "prompt_tokens": 8_600,
                "context_length": 10_000,
                "fraction": 0.86,
            }
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "Context pressure:" in content
    assert "Context pressure is high" in content
    assert "8.6K/10.0K" in content
    assert "artifact references" in content


def test_prompt_includes_cumulative_usage_pressure():
    job = {
        "title": "usage pressure",
        "kind": "generic",
        "objective": "keep a long-running job useful",
        "metadata": {
            "finding_ledger": [{"name": "durable fact"}],
            "source_ledger": [{"source": "local evidence"}],
            "experiment_ledger": [{"title": "trial", "metric_value": 1}],
            "task_queue": [{"title": "done branch", "status": "done", "result": "validated"}],
        },
    }

    content = build_messages(
        job,
        [],
        token_usage={
            "calls": 2_100,
            "prompt_tokens": 21_000_000,
            "completion_tokens": 1_000_000,
            "total_tokens": 22_000_000,
            "latest_prompt_tokens": 10_000,
            "latest_context_length": 262_144,
            "cost": 10.25,
            "has_cost": True,
        },
    )[-1]["content"]

    assert "Usage pressure:" in content
    assert "Cumulative model usage pressure is critical" in content
    assert "calls=2100" in content
    assert "tokens=22.0M" in content
    assert "cost=$10.2500" in content
    assert "high leverage" in content


def test_run_one_step_records_usage_pressure_without_spam(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path), model=ModelConfig(context_length=10_000_000))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep a long-running task efficient", title="usage pressure", kind="generic")
        llm = ScriptedLLM([
            LLMResponse(
                tool_calls=[ToolCall(name="record_lesson", arguments={"lesson": "consolidate before spending more", "category": "strategy"})],
                usage={"prompt_tokens": 1_100_000, "completion_tokens": 100, "total_tokens": 1_100_100, "cost": 1.1},
            ),
            LLMResponse(
                tool_calls=[ToolCall(name="record_lesson", arguments={"lesson": "second consolidation", "category": "strategy"})],
                usage={"prompt_tokens": 300_000, "completion_tokens": 100, "total_tokens": 300_100, "cost": 0.3},
            ),
        ])

        run_one_step(job_id, config=config, db=db, llm=llm)
        run_one_step(job_id, config=config, db=db, llm=llm)

        pressure_events = [
            event
            for event in db.list_events(job_id=job_id, event_types=["agent_message"])
            if event["metadata"].get("kind") == "usage_pressure"
        ]
        assert len(pressure_events) == 1
        assert "Usage pressure watch" in pressure_events[0]["body"]
        job = db.get_job(job_id)
        pressure = job["metadata"]["usage_pressure"]
        assert pressure["band"] == "watch"
        assert pressure["calls"] == 2
        assert pressure["total_tokens"] == 1_400_200
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
    assert len(content) < MAX_WORKER_PROMPT_CHARS
    assert "Next-action constraint:" in content


def test_prompt_timeline_filters_low_signal_tool_noise():
    job = {
        "title": "timeline",
        "kind": "generic",
        "objective": "keep useful context visible",
        "metadata": {},
    }
    timeline = [
        {
            "event_type": "tool_result",
            "title": "web_search",
            "body": f"search noise {index}",
            "metadata": {"status": "completed"},
            "created_at": f"2026-05-01T12:{index:02d}:00+00:00",
        }
        for index in range(20)
    ]
    timeline.extend([
        {
            "event_type": "artifact",
            "title": "Saved durable report",
            "body": "operator-visible output",
            "metadata": {},
            "created_at": "2026-05-01T13:00:00+00:00",
        },
        {
            "event_type": "finding",
            "title": "Useful durable finding",
            "body": "result worth keeping",
            "metadata": {},
            "created_at": "2026-05-01T13:01:00+00:00",
        },
        {
            "event_type": "tool_result",
            "title": "shell_exec",
            "body": "command failed with actionable blocker",
            "metadata": {"status": "failed"},
            "created_at": "2026-05-01T13:02:00+00:00",
        },
    ])

    content = build_messages(job, [], timeline_events=timeline)[-1]["content"]

    assert "Recent visible timeline:" in content
    assert "High-signal timeline counts:" in content
    assert "Saved durable report" in content
    assert "Useful durable finding" in content
    assert "command failed with actionable blocker" in content
    assert "search noise" not in content


def test_prompt_includes_durable_outcome_summary():
    job = {
        "title": "outcomes",
        "kind": "generic",
        "objective": "keep useful durable progress visible",
        "metadata": {},
    }
    events = [
        {
            "event_type": "artifact",
            "title": "Draft checkpoint",
            "body": "",
            "metadata": {},
        },
        {
            "event_type": "finding",
            "title": "Reusable finding",
            "body": "",
            "metadata": {},
        },
        {
            "event_type": "experiment",
            "title": "Quality check",
            "body": "",
            "metadata": {"metric_name": "score", "metric_value": 0.82, "metric_unit": ""},
        },
        {
            "event_type": "tool_result",
            "title": "web_search",
            "body": "web_search query='background' returned 5 results",
            "metadata": {"status": "completed"},
        },
    ]

    content = build_messages(job, [], timeline_events=events)[-1]["content"]
    outcome_section = content.split("Durable outcomes:", 1)[1].split("Ledgers:", 1)[0]

    assert "Outcome counts: 1 outputs 1 findings 1 measurements." in outcome_section
    assert "save: Draft checkpoint" in outcome_section
    assert "find: Reusable finding" in outcome_section
    assert "test: Quality check" in outcome_section
    assert "background" not in outcome_section


def test_emergency_prompt_clipping_repeats_operator_and_next_action():
    job = {"title": "clip", "kind": "generic", "objective": "keep context safe"}
    sections = [(f"Noise {index}", "noise " * 2000) for index in range(90)]
    sections.insert(45, ("Operator context", "Still-active durable operator context: use the corrected target."))
    sections.append(("Next-action constraint", "Next use the validated branch."))

    content = _render_worker_prompt(job, sections=sections)

    assert len(content) <= MAX_WORKER_PROMPT_CHARS
    assert "middle context clipped" in content
    suffix = content.split("middle context clipped", 1)[1]
    assert "Operator context:" in suffix
    assert "use the corrected target" in suffix
    assert "Next-action constraint:" in suffix
    assert "Next use the validated branch" in suffix


def test_build_messages_keeps_rolling_memory_when_not_first():
    job = {"title": "memory order", "kind": "generic", "objective": "keep long-running context stable"}
    memory_entries = [
        {"key": "newer_note", "summary": "newer side note"},
        {"key": "other_note", "summary": "less important side note"},
        {"key": "rolling_state", "summary": "durable rolling state with usage and task progress"},
    ]

    content = build_messages(job, [], memory_entries=memory_entries)[-1]["content"]

    assert "durable rolling state with usage and task progress" in content
    assert "newer side note" in content
    assert "less important side note" not in content


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
                                "next_action": "compare the next concrete variant",
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


def test_measurement_obligation_blocks_operator_acknowledgement_churn(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process", title="measure-ack", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_measurement_obligation": {
                    "source_step_no": 12,
                    "tool": "shell_exec",
                    "metric_candidates": ["2.7 tok/s"],
                    "command": "run benchmark",
                }
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="acknowledge_operator_context",
                        arguments={"message_ids": [], "summary": "acknowledged"},
                    )
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.tool_name == "acknowledge_operator_context"
        assert result.result["error"] == "measurement obligation pending"
    finally:
        db.close()


def test_pending_measurement_narrows_available_tools(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process", title="measure-tools", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_measurement_obligation": {
                    "source_step_no": 12,
                    "tool": "shell_exec",
                    "metric_candidates": ["2.7 tok/s"],
                    "command": "run benchmark",
                }
            },
        )
        llm = CapturingLLM(
            LLMResponse(tool_calls=[
                ToolCall(
                    name="record_experiment",
                    arguments={
                        "title": "measured trial",
                        "status": "measured",
                        "metric_name": "speed",
                        "metric_value": 2.7,
                        "metric_unit": "tok/s",
                        "next_action": "try the next measured branch",
                    },
                )
            ])
        )

        run_one_step(job_id, config=config, db=db, llm=llm)

        tool_names = {tool["function"]["name"] for tool in llm.tools}
        assert {"record_experiment", "record_lesson", "record_tasks"}.issubset(tool_names)
        assert "shell_exec" not in tool_names
        assert "web_search" not in tool_names
        assert "acknowledge_operator_context" not in tool_names
    finally:
        db.close()


def test_pending_evidence_checkpoint_narrows_available_tools(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Account for checkpointed evidence", title="checkpoint-tools", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Checkpoint",
                    "evidence_step_no": 12,
                    "blocked_tool": "shell_exec",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            },
        )
        llm = CapturingLLM(
            LLMResponse(tool_calls=[
                ToolCall(name="record_lesson", arguments={"lesson": "checkpoint accounted for", "category": "memory"})
            ])
        )

        run_one_step(job_id, config=config, db=db, llm=llm)

        tool_names = {tool["function"]["name"] for tool in llm.tools}
        assert "read_artifact" in tool_names
        assert {"record_findings", "record_source", "record_lesson", "record_experiment"}.issubset(tool_names)
        assert "record_tasks" not in tool_names
        assert "shell_exec" not in tool_names
        assert "web_search" not in tool_names
        assert "acknowledge_operator_context" not in tool_names
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


def test_prose_from_timed_command_does_not_create_measurement_obligation(tmp_path):
    class ProseShellRegistry:
        def openai_tools(self):
            return []

        def handle(self, name, args, ctx):
            del args, ctx
            if name == "shell_exec":
                return json.dumps({
                    "success": True,
                    "command": "time cat draft.txt",
                    "returncode": 0,
                    "stdout": 'This draft says "time". 2 examples are listed. It asks readers to rate a story.',
                    "stderr": "",
                })
            return json.dumps({"success": True})

    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a measurable process", title="measure", kind="generic")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "time cat draft.txt"})])]),
            registry=ProseShellRegistry(),
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


def test_prompt_suppresses_stale_negative_lessons_when_positive_durable_evidence_exists():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep facts current",
        "metadata": {
            "finding_ledger": [
                {
                    "name": "Observed local model",
                    "reason": "ModelX-99 appears in the local model list with size 17 GB.",
                }
            ],
            "lessons": [
                {
                    "category": "strategy",
                    "lesson": (
                        "No ModelX-99 model has been successfully downloaded, so keep the download "
                        "branch as the primary blocker before benchmark work."
                    ),
                }
            ],
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "Potentially stale negative lesson suppressed for ModelX-99" in content
    assert "No ModelX-99 model has been successfully downloaded" not in content


def test_prompt_keeps_negative_lessons_when_durable_evidence_is_negative():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep facts current",
        "metadata": {
            "finding_ledger": [
                {
                    "name": "Missing local model",
                    "reason": "ls cannot access ModelX-99: no such file or directory.",
                }
            ],
            "lessons": [
                {
                    "category": "strategy",
                    "lesson": "No ModelX-99 file exists in the checked path; use a different observed source.",
                }
            ],
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "No ModelX-99 file exists" in content
    assert "Potentially stale negative lesson suppressed" not in content


def test_prompt_includes_memory_graph_slice():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep improving the output",
        "metadata": {
            "memory_graph": {
                "nodes": [
                    {
                        "key": "validated-checkpoints",
                        "title": "Validated checkpoints compound progress",
                        "kind": "strategy",
                        "status": "active",
                        "summary": "Save evidence, validate it, then branch from the gap.",
                        "salience": 0.95,
                        "tags": ["progress"],
                        "evidence_refs": ["art_1"],
                    },
                    {
                        "key": "weak-source",
                        "title": "Weak source path",
                        "kind": "source",
                        "status": "deprecated",
                        "summary": "This path produced low-yield repeats.",
                        "salience": 0.1,
                    },
                ],
                "edges": [
                    {
                        "from_key": "validated-checkpoints",
                        "to_key": "weak-source",
                        "relation": "replaces",
                    }
                ],
            }
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "Memory graph:" in content
    assert "Validated checkpoints compound progress" in content
    assert "strategy" in content
    assert "replaces -> weak-source" in content
    assert "art_1" in content


def test_prompt_suppresses_memory_graph_nodes_matching_stale_claim_tokens(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Prefer current durable evidence", title="stale-graph", kind="generic")
        db.append_lesson(
            job_id,
            "Evidence grounding rejected unsupported concrete tokens for record_experiment: E5-2690, v3. Treat matching prior ledger claims as stale.",
            category="mistake",
        )
        db.append_memory_graph_records(
            job_id,
            nodes=[
                {
                    "key": "old-hardware",
                    "title": "Intel Xeon E5-2690 v3 baseline",
                    "kind": "fact",
                    "status": "stable",
                    "summary": "Old baseline that should not enter the prompt after contradiction.",
                },
                {
                    "key": "current-evidence",
                    "title": "Current observed hardware needs verification",
                    "kind": "fact",
                    "status": "active",
                    "summary": "Continue from fresh shell evidence only.",
                },
            ],
        )

        job = db.get_job(job_id)
        content = build_messages(job, db.list_steps(job_id=job_id))[-1]["content"]

        assert "Suppressed 1 stale memory node" in content
        assert "Current observed hardware needs verification" in content
        assert "Intel Xeon E5-2690 v3 baseline" not in content
    finally:
        db.close()


def test_prompt_pushes_memory_graph_consolidation_when_ledgers_exist_without_nodes():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep improving the output",
        "metadata": {
            "lessons": [{"lesson": "Prefer validated checkpoints.", "category": "strategy"}],
            "experiment_ledger": [{"title": "Trial", "status": "measured"}],
            "task_queue": [{"title": "Next branch", "status": "open"}],
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "No memory graph yet" in content
    assert "Durable ledgers already contain 3 reusable item" in content
    assert "record_memory_graph" in content


def test_prompt_adds_memory_consolidation_guard_when_graph_lags_ledgers():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep improving the output",
        "metadata": {
            "lessons": [
                {"lesson": "Use validated checkpoints.", "category": "strategy"},
                {"lesson": "Reject low-yield branches.", "category": "strategy"},
            ],
            "experiment_ledger": [{"title": "Trial", "status": "measured"}],
            "finding_ledger": [{"name": "Finding A"}, {"name": "Finding B"}],
            "source_ledger": [{"source": "source:a"}],
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "Memory consolidation guard:" in content
    assert "durable_records=6" in content
    assert "record_memory_graph" in content


def test_prompt_adds_research_balance_guard_for_execution_without_sources():
    job = {
        "title": "workflow builder",
        "kind": "generic",
        "objective": "build a durable workflow and keep improving it",
        "metadata": {
            "experiment_ledger": [{"title": "Validation check", "status": "measured"}],
        },
    }
    steps = [
        {
            "step_no": index,
            "kind": "tool",
            "tool_name": "shell_exec",
            "status": "completed",
            "input": {"arguments": {"command": f"echo branch-{index}"}},
        }
        for index in range(1, 7)
    ]

    content = build_messages(job, steps)[-1]["content"]

    assert "Research balance guard:" in content
    assert "execution-heavy" in content
    assert "sources=0" in content
    assert "record_source" in content


def test_prompt_research_balance_guard_clears_when_sources_exist():
    job = {
        "title": "workflow builder",
        "kind": "generic",
        "objective": "build a durable workflow and keep improving it",
        "metadata": {
            "source_ledger": [{"source": "project docs"}],
            "experiment_ledger": [{"title": "Validation check", "status": "measured"}],
        },
    }
    steps = [
        {
            "step_no": index,
            "kind": "tool",
            "tool_name": "shell_exec",
            "status": "completed",
            "input": {"arguments": {"command": f"echo branch-{index}"}},
        }
        for index in range(1, 7)
    ]

    content = build_messages(job, steps)[-1]["content"]

    assert "Recent work is execution-heavy" not in content


def test_run_one_step_blocks_execution_when_research_balance_is_missing(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Build a durable workflow and keep improving it",
            title="research-balance",
            kind="generic",
            metadata={"experiment_ledger": [{"title": "Validation check", "status": "measured"}]},
        )
        run_id = db.start_run(job_id, model="test")
        for index in range(6):
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="shell_exec",
                input_data={"arguments": {"command": f"python branch_{index}.py"}},
            )
            db.finish_step(step_id, status="completed", output_data={"success": True, "stdout": "ok"})
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "python continue_branch.py"})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "research balance required"
        assert result.result["blocked_tool"] == "shell_exec"
        assert "record_source" in result.result["guidance"]
    finally:
        db.close()


def test_run_one_step_blocks_durable_records_with_unsupported_concrete_claims(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Optimize a measurable process on observed hardware", title="grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": "GPU: AMD Device 7590\nCPU: AMD Ryzen 9 7900X\nMemory: 93Gi\n",
                "stderr": "",
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_roadmap",
                        arguments={
                            "title": "Performance roadmap",
                            "status": "active",
                            "current_milestone": "Environment",
                            "metadata": {
                                "hardware": "NVIDIA GTX 970 with CUDA and i5-8400 CPU",
                                "claim": "Use CUDA-first optimization.",
                            },
                            "milestones": [],
                        },
                    )
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "evidence grounding required"
        assert result.result["blocked_tool"] == "record_roadmap"
        assert "GTX" in result.result["evidence_grounding"]["unsupported_tokens"]
        lessons = db.get_job(job_id)["metadata"]["lessons"]
        assert any("GTX" in lesson["lesson"] and "stale" in lesson["lesson"] for lesson in lessons)
    finally:
        db.close()


def test_record_experiment_blocks_unsupported_proper_noun_hardware_claims(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Record exact observed environment", title="grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "NO_NVIDIA_GPU\n"
                    "GPU: Advanced Micro Devices Device 7590\n"
                    "Threads: 24\n"
                    "CPU: AMD Ryzen 9 7900X 12-Core Processor\n"
                ),
            },
        )
        db.finish_run(run_id, "completed")

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_experiment",
                        arguments={
                            "title": "Environment Baseline - Hardware Runtime Facts",
                            "status": "measured",
                            "metric_name": "cpu_threads",
                            "metric_value": 16,
                            "metric_unit": "threads",
                            "result": "Environment baseline captured. Hardware: Dual Intel Xeon CPUs, 16 threads total.",
                            "next_action": "Continue from exact observed hardware facts.",
                        },
                    )
                ])
            ]),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "evidence grounding required"
        assert {"Dual", "Intel", "Xeon"} <= set(blocked.result["evidence_grounding"]["unsupported_tokens"])
    finally:
        db.close()


def test_record_experiment_allows_supported_proper_noun_hardware_claims(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Record exact observed environment", title="grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "NO_NVIDIA_GPU\n"
                    "GPU: Advanced Micro Devices Device 7590\n"
                    "Threads: 24\n"
                    "CPU: AMD Ryzen 9 7900X 12-Core Processor\n"
                ),
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_experiment",
                        arguments={
                            "title": "Environment Baseline - Hardware Runtime Facts",
                            "status": "measured",
                            "metric_name": "cpu_threads",
                            "metric_value": 24,
                            "metric_unit": "threads",
                            "result": "Environment baseline captured. Hardware: AMD Ryzen 9 7900X, 24 threads total.",
                            "next_action": "Continue from exact observed hardware facts.",
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        experiment = db.get_job(job_id)["metadata"]["experiment_ledger"][0]
        assert "AMD Ryzen 9 7900X" in experiment["result"]
    finally:
        db.close()


def test_record_lesson_blocks_negative_claim_that_conflicts_with_positive_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep exact observed facts durable", title="lesson-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "NAME ID SIZE MODIFIED\n"
                    "ModelX-99 a50eda8ed977 17 GB 2 weeks ago\n"
                    "OtherModel 69492d6584c5 14 GB 2 months ago\n"
                ),
            },
        )
        db.finish_run(run_id, "completed")

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_lesson",
                        arguments={
                            "category": "strategy",
                            "lesson": (
                                "No ModelX-99 model has been successfully downloaded, so keep the download branch "
                                "as the primary blocker before any benchmark work."
                            ),
                        },
                    )
                ])
            ]),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "evidence grounding required"
        grounding = blocked.result["evidence_grounding"]
        assert "ModelX-99" in grounding["unsupported_tokens"]
        assert grounding["negative_claim_conflicts"][0]["token"] == "ModelX-99"
    finally:
        db.close()


def test_record_lesson_allows_negative_claim_when_evidence_is_also_negative(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep exact observed facts durable", title="lesson-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": "ls: cannot access '/tmp/ModelX-99.gguf': No such file or directory\n",
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_lesson",
                        arguments={
                            "category": "strategy",
                            "lesson": (
                                "No ModelX-99 file exists in the checked path, so the next branch must use a "
                                "different observed source or record the missing file as blocked."
                            ),
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        lesson = db.get_job(job_id)["metadata"]["lessons"][0]
        assert "ModelX-99" in lesson["lesson"]
    finally:
        db.close()


def test_record_findings_blocks_negative_file_pattern_that_conflicts_with_positive_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep file discovery evidence exact", title="file-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "/srv/data/WidgetModel-99-Q4.foo\n"
                    "/tmp/results/report.alpha\n"
                    "/var/cache/other-file.foo\n"
                ),
            },
        )
        db.finish_run(run_id, "completed")

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_findings",
                        arguments={
                            "findings": [
                                {
                                    "name": "No .foo files found on filesystem",
                                    "category": "environment_baseline",
                                    "status": "confirmed",
                                    "reason": "Shell search found zero .foo files larger than 1MB anywhere on the system.",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "evidence grounding required"
        grounding = blocked.result["evidence_grounding"]
        assert ".foo" in grounding["unsupported_tokens"]
        assert grounding["negative_claim_conflicts"][0]["token"] == ".foo"
    finally:
        db.close()


def test_record_findings_allows_negative_file_pattern_when_evidence_is_negative(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep file discovery evidence exact", title="file-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": "find: '/tmp/WidgetModel-99.foo': No such file or directory\n",
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_findings",
                        arguments={
                            "findings": [
                                {
                                    "name": "No .foo file exists in the checked path",
                                    "category": "environment_baseline",
                                    "status": "confirmed",
                                    "reason": "The shell output says the checked .foo path does not exist.",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        findings = db.get_job(job_id)["metadata"]["finding_ledger"]
        assert findings[0]["name"] == "No .foo file exists in the checked path"
    finally:
        db.close()


def test_run_one_step_marks_contradicted_negative_finding_stale(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep durable findings aligned with fresh evidence", title="stale-finding", kind="generic")
        db.append_finding_record(
            job_id,
            name="No .foo files found",
            category="environment_baseline",
            reason="Shell search found zero .foo files anywhere in the checked filesystem.",
            status="confirmed",
        )
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "discovery results from current filesystem scan:\n"
                    "/srv/data/WidgetModel-99-Q4.foo\n"
                    "/var/cache/other-file.foo\n"
                ),
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_lesson",
                        arguments={
                            "category": "strategy",
                            "lesson": "Fresh file-discovery evidence should override older absence claims.",
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        job = db.get_job(job_id)
        stale_records = job["metadata"].get("stale_negative_records")
        assert isinstance(stale_records, list)
        assert stale_records[0]["kind"] == "finding"
        assert stale_records[0]["token"] == ".foo"

        from nipux_cli.worker_prompt_context import _ledgers_for_prompt

        ledgers = _ledgers_for_prompt(job)
        assert "Contradicted negative findings suppressed" in ledgers
        assert "Suppressed 1 stale finding" in ledgers
    finally:
        db.close()


def test_record_lesson_allows_generic_strategy_without_concrete_facts(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a workflow", title="lesson-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(step_id, status="completed", output_data={"success": True, "stdout": "branch stalled\n"})
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_lesson",
                        arguments={
                            "category": "strategy",
                            "lesson": "When a branch stalls, pivot to the next measurable action instead of adding more notes.",
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
    finally:
        db.close()


def test_record_lesson_allows_positive_checkpoint_summary_with_new_concrete_terms(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Summarize a broad checkpoint", title="lesson-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={"success": True, "stdout": "checkpoint read and accounting required\n"},
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_lesson",
                        arguments={
                            "category": "memory",
                            "lesson": (
                                "Recording checkpoint context says PackageManager-42 and RuntimeProbe-7 should stay "
                                "available for the next branch, but no final benchmark decision has been made."
                            ),
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
    finally:
        db.close()


def test_record_findings_blocks_single_unsupported_identifier(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Record only observed identifiers", title="grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "Observed candidate list from tool output. "
                    "The source contains AlphaCandidate and BetaCandidate with ordinary text evidence. "
                    "No generated opaque identifiers are present in this evidence."
                )
                * 12,
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="record_findings", arguments={
                    "findings": [{
                        "name": "WWHHH5 generated candidate",
                        "category": "test",
                        "reason": "Observed candidate list needs follow-up, but this identifier was not in evidence.",
                        "status": "new",
                    }]
                })])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "evidence grounding required"
        assert result.result["evidence_grounding"]["unsupported_tokens"] == ["WWHHH5"]
    finally:
        db.close()


def test_evidence_grounding_ignores_record_schema_keys(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Record observed setup status", title="grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={"success": True, "stdout": "Python 3 is installed. curl is available. No token file was found."},
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_experiment",
                        arguments={
                            "title": "Setup status",
                            "status": "measured",
                            "metric_name": "ready_components",
                                "metric_value": 1,
                                "config": {"python_3_installed": True, "curl_available": True},
                                "result": "Python 3 is installed and curl is available.",
                                "next_action": "record remaining setup gaps or proceed to the next validation",
                            },
                        )
                    ])
                ]),
        )

        assert result.status == "completed"
        experiment = db.get_job(job_id)["metadata"]["experiment_ledger"][0]
        assert experiment["config"]["python_3_installed"] is True
    finally:
        db.close()


def test_run_one_step_blocks_memory_graph_with_unsupported_claims(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Consolidate observed facts", title="memory-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": "GPU: AMD Device 7590\nCPU: AMD Ryzen 9 7900X\nMemory: 93Gi\n",
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_memory_graph",
                        arguments={
                            "nodes": [
                                {
                                    "key": "hardware",
                                    "kind": "fact",
                                    "title": "NVIDIA GTX 970 CUDA hardware",
                                    "summary": "The machine has NVIDIA GTX 970 CUDA hardware.",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "evidence grounding required"
        assert result.result["blocked_tool"] == "record_memory_graph"
    finally:
        db.close()


def test_run_one_step_allows_memory_graph_identifier_labels_without_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Consolidate abstract graph labels", title="memory-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "Current observation: AMD Ryzen 9 7900X host with fresh API discovery evidence. "
                    "The next branch is to convert existing source evidence into a download decision."
                ),
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_memory_graph",
                        arguments={
                            "edges": [
                                {
                                    "from_key": "decision-q4-km-primary",
                                    "relation": "informs",
                                    "to_key": "question-download-q4-km-url",
                                },
                                {
                                    "from_key": "skill-api-download-pattern",
                                    "relation": "supports",
                                    "to_key": "milestone-direct-url-download",
                                },
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_memory_graph"
    finally:
        db.close()


def test_run_one_step_still_blocks_stale_memory_graph_key_claims(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Do not reintroduce stale graph labels",
            title="memory-grounding",
            kind="generic",
            metadata={"unsupported_claim_tokens": ["XeonE5-2690"]},
        )
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "Current observation: AMD Ryzen 9 7900X host with no legacy CPU marker in fresh evidence. "
                    "Durable memory must not reuse unsupported old hardware claims."
                ),
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_memory_graph",
                        arguments={
                            "edges": [
                                {
                                    "from_key": "XeonE5-2690",
                                    "relation": "constrains",
                                    "to_key": "current-plan",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "evidence grounding required"
        assert "XeonE5-2690" in result.result["evidence_grounding"]["unsupported_tokens"]
    finally:
        db.close()


def test_run_one_step_allows_memory_graph_grounded_in_durable_records(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Consolidate durable facts", title="memory-grounded-ledger", kind="generic")
        db.append_finding_record(
            job_id,
            name="Artifact cache includes Package_A-2.7.1 and backend XYZ123",
            category="environment_fact",
            reason="A saved checkpoint established Package_A-2.7.1 and backend XYZ123 as available options.",
            metadata={"evidence_artifact": "art_env"},
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_memory_graph",
                        arguments={
                            "nodes": [
                                {
                                    "key": "package-a",
                                    "kind": "fact",
                                    "title": "Package_A-2.7.1 via backend XYZ123",
                                    "summary": "Durable finding says Package_A-2.7.1 is available through backend XYZ123.",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_memory_graph"
    finally:
        db.close()


def test_run_one_step_blocks_memory_graph_grounded_only_in_stale_records(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Consolidate durable facts",
            title="memory-stale-ledger",
            kind="generic",
            metadata={"unsupported_claim_tokens": ["XeonE5-2690"]},
        )
        db.append_finding_record(
            job_id,
            name="Artifact cache includes XeonE5-2690",
            category="environment_fact",
            reason="Older ledger record mentioned XeonE5-2690.",
            metadata={"evidence_artifact": "art_old"},
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_memory_graph",
                        arguments={
                            "nodes": [
                                {
                                    "key": "package-a",
                                    "kind": "fact",
                                    "title": "XeonE5-2690",
                                    "summary": "XeonE5-2690 is still valid.",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "evidence grounding required"
        assert "XeonE5-2690" in result.result["evidence_grounding"]["unsupported_tokens"]
    finally:
        db.close()


def test_run_one_step_allows_stale_token_when_fresh_evidence_revalidates_it(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Revalidate durable facts",
            title="memory-stale-revalidated",
            kind="generic",
            metadata={"unsupported_claim_tokens": ["XeonE5-2690"]},
        )
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={"success": True, "stdout": "Fresh probe: CPU marker XeonE5-2690 is visible in this environment."},
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_memory_graph",
                        arguments={
                            "nodes": [
                                {
                                    "key": "fresh-cpu",
                                    "kind": "fact",
                                    "title": "XeonE5-2690",
                                    "summary": "Fresh shell evidence revalidated XeonE5-2690.",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_memory_graph"
    finally:
        db.close()


def test_run_one_step_allows_durable_records_grounded_in_read_artifact(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Record facts from saved evidence", title="grounded-read", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="read_artifact",
            input_data={"arguments": {"artifact_id": "art_checkpoint"}},
        )
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "content": (
                    "Environment evidence: CPU Intel Xeon E5-2690 v3, architecture x86_64, "
                    "memory 62.8G total, no NVIDIA GPU visible from nvidia-smi. "
                    "This content is the source for durable records."
                ),
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_findings",
                        arguments={
                            "findings": [
                                {
                                    "name": "Intel Xeon E5-2690 v3 x86_64 environment",
                                    "category": "hardware_fact",
                                    "reason": "Saved checkpoint states CPU Intel Xeon E5-2690 v3, x86_64, memory 62.8G total, and no NVIDIA GPU visible.",
                                    "evidence_artifact": "art_checkpoint",
                                }
                            ]
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        findings = db.get_job(job_id)["metadata"]["finding_ledger"]
        assert findings[0]["name"] == "Intel Xeon E5-2690 v3 x86_64 environment"
    finally:
        db.close()


def test_run_one_step_scopes_grounding_to_cited_step(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Record facts from cited evidence", title="cited-grounding", kind="generic")
        run_id = db.start_run(job_id, model="test")
        old_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            old_step,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "Old evidence: Intel Xeon E5-2690 v3 with 62.8G memory. "
                    "This is intentionally stale evidence from an earlier step and should not validate step #2."
                ),
            },
        )
        new_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            new_step,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "Current evidence: AMD Ryzen 9 7900X with 93Gi memory and AMD GPU. "
                    "This newer cited step is the only source that should ground claims citing step #2."
                ),
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="write_artifact",
                        arguments={
                            "title": "Cited baseline",
                            "summary": "Baseline from step #2.",
                            "content": "From step #2: Intel Xeon E5-2690 v3 with 62.8G memory.",
                            "artifact_type": "text",
                        },
                    )
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "evidence grounding required"
        assert "E5-2690" in result.result["evidence_grounding"]["unsupported_tokens"]
        assert result.result["evidence_grounding"]["evidence_steps"] == [2]
        assert "E5-2690" in db.get_job(job_id)["metadata"]["unsupported_claim_tokens"]
    finally:
        db.close()


def test_prompt_shows_evidence_grounding_tokens_after_block(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Use only observed evidence", title="grounding-prompt", kind="generic")
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        db.finish_step(
            step_id,
            status="blocked",
            output_data={
                "success": True,
                "recoverable": True,
                "error": "evidence grounding required",
                "evidence_grounding": {"unsupported_tokens": ["NVIDIA", "Xeon", "AVX-512"]},
            },
        )
        job = db.get_job(job_id)
        content = build_messages(job, db.list_steps(job_id=job_id))[-1]["content"]

        assert "unsupported=NVIDIA, Xeon, AVX-512" in content
        assert "use only tokens present in recent observed evidence" in content
    finally:
        db.close()


def test_prompt_suppresses_findings_matching_stale_claim_tokens(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Prefer current durable evidence", title="stale-ledger", kind="generic")
        db.append_finding_record(job_id, name="Intel Xeon E5-2690 v3 baseline", category="hardware")
        db.append_finding_record(job_id, name="AMD Ryzen 9 7900X baseline", category="hardware")
        db.append_lesson(
            job_id,
            "Evidence grounding rejected unsupported concrete tokens for record_experiment: E5-2690, v3, RAM. Treat matching prior ledger claims as stale.",
            category="mistake",
        )

        job = db.get_job(job_id)
        content = build_messages(job, db.list_steps(job_id=job_id))[-1]["content"]

        assert "Unsupported/stale claim tokens to avoid until re-verified: [unsupported-stale-claim]" in content
        assert "Suppressed 1 stale finding" in content
        assert "AMD Ryzen 9 7900X baseline" in content
        assert "Intel Xeon E5-2690 v3 baseline" not in content
    finally:
        db.close()


def test_prompt_prioritizes_validation_for_recent_candidate_file_paths(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Validate a discovered runtime file", title="candidate-file", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "task_queue": [
                    {
                        "title": "Run baseline benchmark with the discovered file",
                        "status": "open",
                        "contract": "experiment",
                        "acceptance_criteria": "Benchmark command uses a validated file path.",
                        "evidence_needed": "Shell output showing file size and benchmark result.",
                    }
                ]
            },
        )
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    "candidate files:\n"
                    "/srv/models/ExampleModel-Q4.foo\n"
                    "/srv/models/sidecar.txt\n"
                ),
            },
        )
        db.finish_run(run_id, "completed")

        content = build_messages(db.get_job(job_id), db.list_steps(job_id=job_id))[-1]["content"]

        assert "Candidate file discovery:" in content
        assert "/srv/models/ExampleModel-Q4.foo" in content
        assert "Validate likely candidates with shell_exec" in content
    finally:
        db.close()


def test_prompt_prioritizes_structured_candidate_file_paths(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Validate a discovered remote file", title="candidate-file", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "task_queue": [
                    {
                        "title": "Download and validate a candidate file",
                        "status": "open",
                        "contract": "action",
                        "acceptance_criteria": "A candidate file path is selected and validated before use.",
                        "evidence_needed": "Shell output with size, hash, or validation metadata.",
                    }
                ]
            },
        )
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            step_id,
            status="completed",
            output_data={
                "success": True,
                "stdout": (
                    '[{"type":"file","size":123456789,"path":"ExampleModel-Q4.foo"},'
                    '{"type":"file","size":42,"path":".gitattributes"}]'
                ),
            },
        )
        db.finish_run(run_id, "completed")

        content = build_messages(db.get_job(job_id), db.list_steps(job_id=job_id))[-1]["content"]

        assert "Candidate file discovery:" in content
        assert "ExampleModel-Q4.foo" in content
        assert "Validate likely candidates with shell_exec" in content
    finally:
        db.close()


def test_prompt_resurfaces_durable_candidate_file_paths(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Validate a remembered file candidate", title="durable-candidate", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "task_queue": [
                    {
                        "title": "Validate remembered file path",
                        "status": "open",
                        "contract": "action",
                        "acceptance_criteria": "A candidate file path is validated before use.",
                        "evidence_needed": "Shell output with file size or hash.",
                    }
                ],
                "experiment_ledger": [
                    {
                        "title": "Prior candidate discovery",
                        "result": "A previous branch listed /opt/models/Remembered-Model-Q4.foo as a candidate.",
                        "next_action": "Validate /opt/models/Remembered-Model-Q4.foo before declaring no usable file.",
                    }
                ],
            },
        )

        content = build_messages(db.get_job(job_id), db.list_steps(job_id=job_id))[-1]["content"]

        assert "Candidate file discovery:" in content
        assert "Durable records mention candidate file paths" in content
        assert "/opt/models/Remembered-Model-Q4.foo" in content
        assert "Treat durable-record candidates as leads until revalidated" in content
    finally:
        db.close()


def test_prompt_filters_stale_generated_and_objective_tokens(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Optimize Qwen3.6-27B GGUF throughput", title="qwen job", kind="generic")
        db.append_lesson(
            job_id,
            (
                "Evidence grounding rejected unsupported concrete tokens for record_experiment: "
                "Qwen3.6-27B-GGUF, JSON, shell_exec_step_1037, timeout_after_300s, E5-2690. "
                "Treat matching prior ledger claims as stale."
            ),
            category="mistake",
        )
        db.append_finding_record(job_id, name="Qwen3.6-27B-GGUF source", category="source")
        db.append_finding_record(job_id, name="Intel Xeon E5-2690 baseline", category="hardware")

        content = build_messages(db.get_job(job_id), db.list_steps(job_id=job_id))[-1]["content"]

        assert "Qwen3.6-27B-GGUF" in content
        assert "JSON" not in content
        assert "shell_exec_step_1037" not in content
        assert "timeout_after_300s" not in content
        assert "Unsupported/stale claim tokens to avoid until re-verified: [unsupported-stale-claim]" in content
        assert "Intel Xeon E5-2690 baseline" not in content
    finally:
        db.close()


def test_prompt_redacts_stale_tokens_from_recent_state(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Prefer current durable evidence",
            title="stale-recent-state",
            kind="generic",
            metadata={"unsupported_claim_tokens": ["E5-2690", "v3"]},
        )
        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="record_findings",
            input_data={"arguments": {"finding": "Old CPU claim: Intel Xeon E5-2690 v3"}},
        )
        db.finish_step(
            step_id,
            status="blocked",
            output_data={"success": False, "error": "evidence grounding required"},
            summary="blocked record_findings; Intel Xeon E5-2690 v3 unsupported",
        )
        db.finish_run(run_id, "blocked")

        content = build_messages(db.get_job(job_id), db.list_steps(job_id=job_id))[-1]["content"]

        assert "E5-2690" not in content
        assert "[unsupported-stale-claim]" in content
    finally:
        db.close()


def test_prompt_redacts_older_stale_tokens_from_task_queue(tmp_path):
    stale_tail = [f"GPU{i}X" for i in range(60)]
    job = {
        "title": "stale task cleanup",
        "kind": "generic",
        "objective": "use current evidence",
        "metadata": {
            "unsupported_claim_tokens": ["E5-2690", *stale_tail],
            "task_queue": [
                {
                    "title": "Record old baseline",
                    "status": "active",
                    "priority": 10,
                    "goal": "Record CPU: Dual Intel Xeon E5-2690 v3 from old evidence.",
                    "output_contract": "experiment",
                }
            ],
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "E5-2690" not in content
    assert "[unsupported-stale-claim]" in content


def test_run_one_step_requires_accounting_after_auto_checkpoint_read(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Convert evidence checkpoints into progress", title="checkpoint", kind="generic")
        run_id = db.start_run(job_id, model="test")
        checkpoint_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            checkpoint_step,
            status="blocked",
            output_data={
                "success": True,
                "error": "artifact required before more research",
                "auto_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "path": str(tmp_path / "checkpoint.md"),
                    "title": "Auto Evidence Checkpoint after step 1",
                    "evidence_step": "step_evidence",
                    "blocked_tool": "shell_exec",
                },
            },
        )
        read_step = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="read_artifact",
            input_data={"arguments": {"artifact_id": "art_checkpoint"}},
        )
        db.finish_step(read_step, status="completed", output_data={"success": True, "content": "evidence"})
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "echo more discovery"})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "evidence checkpoint accounting required"
        assert result.result["blocked_tool"] == "shell_exec"
    finally:
        db.close()


def test_run_one_step_reads_checkpoint_before_batched_branch_work(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Convert evidence checkpoints into progress", title="checkpoint", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "evidence_step_no": 1,
                    "blocked_tool": "shell_exec",
                }
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="shell_exec", arguments={"command": "echo more discovery"}),
                    ToolCall(name="read_artifact", arguments={"artifact_id": "art_checkpoint"}),
                ])
            ]),
            registry=SuccessRegistry(),
        )

        tool_steps = [step for step in db.list_steps(job_id=job_id) if step.get("kind") == "tool"]
        assert [step["tool_name"] for step in tool_steps[-2:]] == ["read_artifact", "shell_exec"]
        assert result.status == "blocked"
        assert result.result["error"] == "evidence checkpoint accounting required"
        assert result.result["blocked_tool"] == "shell_exec"
        assert result.result["checkpoint_already_read"] is True
        pending = db.get_job(job_id)["metadata"]["pending_evidence_checkpoint"]
        assert pending["read_at"]
    finally:
        db.close()


def test_run_one_step_accounts_checkpoint_before_batched_branch_work(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Convert evidence checkpoints into progress", title="checkpoint", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "read_at": "2026-01-01T00:00:00+00:00",
                    "evidence_step_no": 1,
                    "blocked_tool": "shell_exec",
                }
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="shell_exec", arguments={"command": "echo more discovery"}),
                    ToolCall(name="record_lesson", arguments={"lesson": "checkpoint accounted", "category": "strategy"}),
                ])
            ]),
            registry=SuccessRegistry(),
        )

        tool_steps = [step for step in db.list_steps(job_id=job_id) if step.get("kind") == "tool"]
        assert [step["tool_name"] for step in tool_steps[-2:]] == ["record_lesson", "shell_exec"]
        assert result.status == "completed"
        assert result.tool_name == "shell_exec"
        pending = db.get_job(job_id)["metadata"]["pending_evidence_checkpoint"]
        assert pending["resolved_at"]
        assert pending["resolved_by_tool"] == "record_lesson"
    finally:
        db.close()


def test_run_one_step_treats_guard_recovery_as_checkpoint_accounting(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Convert evidence checkpoints into progress", title="checkpoint", kind="generic")
        run_id = db.start_run(job_id, model="test")
        checkpoint_step = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
        db.finish_step(
            checkpoint_step,
            status="blocked",
            output_data={
                "success": True,
                "error": "artifact required before more research",
                "auto_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "path": str(tmp_path / "checkpoint.md"),
                    "title": "Auto Evidence Checkpoint after step 1",
                    "evidence_step": "step_evidence",
                    "blocked_tool": "shell_exec",
                },
            },
        )
        read_step = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="read_artifact",
            input_data={"arguments": {"artifact_id": "art_checkpoint"}},
        )
        db.finish_step(read_step, status="completed", output_data={"success": True, "content": "evidence"})
        guard_step = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="read_artifact",
            input_data={"arguments": {"artifact_id": "art_checkpoint"}},
        )
        db.finish_step(
            guard_step,
            status="blocked",
            output_data={
                "success": True,
                "recoverable": True,
                "error": "evidence checkpoint accounting required",
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "checkpoint_read": True,
                },
            },
        )
        recovery_step = db.add_step(job_id=job_id, run_id=run_id, kind="recovery", tool_name="guard_recovery")
        db.finish_step(
            recovery_step,
            status="completed",
            output_data={
                "success": True,
                "lesson": {"lesson": "Open a task after repeated guard blocks."},
                "task": {"title": "Resolve guard"},
            },
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "echo more discovery"})])
            ]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "shell_exec"
    finally:
        db.close()


def test_checkpoint_resolution_tool_bypasses_measured_progress_guard(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Optimize benchmark speed", title="checkpoint-measure", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "read_at": "2026-01-01T00:00:00+00:00",
                    "evidence_step_no": 1,
                    "blocked_tool": "shell_exec",
                }
            },
        )
        run_id = db.start_run(job_id, model="fake")
        for index in range(18):
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec")
            db.finish_step(step_id, status="completed", output_data={"success": True, "stdout": f"probe {index}"})
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_source",
                        arguments={
                            "source": "file:///tmp/checkpoint",
                            "source_type": "checkpoint",
                            "outcome": "checkpoint accounted before more benchmark work",
                        },
                    )
                ])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_source"
        pending = db.get_job(job_id)["metadata"]["pending_evidence_checkpoint"]
        assert pending["resolved_by_tool"] == "record_source"
    finally:
        db.close()


def test_run_one_step_persists_checkpoint_obligation_until_accounted(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Convert evidence checkpoints into progress", title="checkpoint", kind="generic")
        db.update_job_metadata(
            job_id,
            {
                "pending_evidence_checkpoint": {
                    "artifact_id": "art_checkpoint",
                    "title": "Auto Evidence Checkpoint after step 1",
                    "path": str(tmp_path / "checkpoint.md"),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "evidence_step": "step_evidence",
                    "evidence_step_no": 1,
                    "blocked_tool": "shell_exec",
                }
            },
        )

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "echo more discovery"})])
            ]),
        )
        assert blocked.status == "blocked"
        assert blocked.result["error"] == "evidence checkpoint accounting required"

        accounted = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="record_lesson",
                        arguments={
                            "lesson": "The checkpoint contains only diagnostic setup evidence; record it and move to the next concrete branch.",
                            "category": "strategy",
                        },
                    )
                ])
            ]),
        )
        assert accounted.status == "completed"
        pending = db.get_job(job_id)["metadata"]["pending_evidence_checkpoint"]
        assert pending["resolved_at"]
        assert pending["resolved_by_tool"] == "record_lesson"
    finally:
        db.close()


def test_run_one_step_blocks_branch_work_when_memory_graph_needs_consolidation(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep improving durable work", title="memory")
        db.append_lesson(job_id, "Use validated checkpoints.", category="strategy")
        db.append_lesson(job_id, "Reject low-yield branches.", category="strategy")
        db.append_finding_record(job_id, name="Finding A")
        db.append_finding_record(job_id, name="Finding B")
        db.append_source_record(job_id, "source:a")
        db.append_experiment_record(job_id, title="Trial", status="measured")
        llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more research"})])])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "blocked"
        assert result.result["error"] == "memory graph consolidation required"
        assert result.result["blocked_tool"] == "web_search"
    finally:
        db.close()


def test_run_one_step_allows_memory_graph_consolidation_when_guard_is_active(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep improving durable work", title="memory")
        db.append_lesson(job_id, "Use validated checkpoints.", category="strategy")
        db.append_lesson(job_id, "Reject low-yield branches.", category="strategy")
        db.append_finding_record(job_id, name="Finding A")
        db.append_finding_record(job_id, name="Finding B")
        db.append_source_record(job_id, "source:a")
        db.append_experiment_record(job_id, title="Trial", status="measured")
        llm = ScriptedLLM([
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="record_memory_graph",
                        arguments={
                            "nodes": [
                                {
                                    "key": "validated-checkpoints",
                                    "kind": "strategy",
                                    "title": "Validated checkpoints",
                                    "summary": "Use measured checkpoints to decide the next branch.",
                                    "salience": 0.9,
                                }
                            ]
                        },
                    )
                ]
            )
        ])

        result = run_one_step(job_id, config=config, db=db, llm=llm)

        assert result.status == "completed"
        assert result.tool_name == "record_memory_graph"
        graph = db.get_job(job_id)["metadata"]["memory_graph"]
        assert graph["nodes"][0]["key"] == "validated-checkpoints"
    finally:
        db.close()


def test_prompt_includes_activity_stagnation_context():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep making durable progress",
        "metadata": {
            "activity_checkpoint_streak": 3,
            "last_checkpoint_counts": {
                "findings": 1,
                "sources": 2,
                "tasks": 4,
                "experiments": 0,
                "lessons": 1,
                "milestones": 0,
            },
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "Activity stagnation" in content
    assert "activity_checkpoint_streak=3" in content
    assert "Recent checkpoints show activity without durable progress" in content


def test_prompt_includes_task_planning_guard_context():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep making durable progress",
        "metadata": {
            "task_planning_checkpoint_streak": 2,
            "task_queue": [
                {"title": "Plan branch", "status": "open"},
                {"title": "Executed branch", "status": "done"},
            ],
        },
    }

    content = build_messages(job, [])[-1]["content"]

    assert "Task planning guard" in content
    assert "task_only_checkpoints=2" in content
    assert "Do not create more new open tasks next" in content


def test_prompt_includes_durable_yield_pressure():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "keep making durable progress",
        "metadata": {},
    }
    steps = [
        {
            "step_no": index,
            "kind": "tool",
            "status": "completed",
            "tool_name": "web_search",
            "summary": f"search {index}",
        }
        for index in range(1, 31)
    ]

    content = build_messages(job, steps)[-1]["content"]

    assert "Durable progress yield" in content
    assert "No durable progress records after 30 completed actions" in content
    assert "record findings/source/experiment/lesson/roadmap progress" in content


def test_prompt_includes_finding_source_ledgers_and_reflections():
    job = {
        "title": "research",
        "kind": "generic",
        "objective": "find research",
        "metadata": {
            "finding_ledger": [{"name": "Acme Finding", "category": "example category", "location": "Toronto", "score": 0.8}],
            "task_queue": [{"title": "Explore primary sources", "status": "open", "priority": 5, "goal": "Find evidence"}],
            "source_ledger": [{"source": "https://example.com", "source_type": "web_source", "usefulness_score": 0.9, "yield_count": 3}],
            "reflections": [{"summary": "Primary source map is working", "strategy": "Try archival sources next"}],
        },
    }

    messages = build_messages(job, [])

    content = messages[-1]["content"]
    assert "Finding ledger: 1 unique candidates." in content
    assert "Acme Finding" in content
    assert "Explore primary sources" in content
    assert "https://example.com" in content
    assert "Primary source map is working" in content


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
    assert "score=3.5 units" in content
    assert "Next-action constraint:" in content
    assert "latest measured experiment selected a concrete next action" in content
    assert "try another independent variant" in content


def test_delivery_experiment_next_action_blocks_unrelated_research(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a generic deliverable", title="deliverable", kind="generic")
        db.update_job_metadata(job_id, {
            "experiment_ledger": [{
                "title": "deliverable gap",
                "status": "measured",
                "metric_name": "coverage",
                "metric_value": 0.25,
                "metric_unit": "ratio",
                "next_action": "merge the measured output into the deliverable file",
            }],
        })

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more background"})])]),
            registry=SuccessRegistry(),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "experiment next action pending"
        assert "merge the measured output" in result.result["experiment_next_action"]["next_action"]
    finally:
        db.close()


def test_research_experiment_next_action_allows_research(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a generic deliverable", title="deliverable", kind="generic")
        db.update_job_metadata(job_id, {
            "experiment_ledger": [{
                "title": "source gap",
                "status": "measured",
                "metric_name": "coverage",
                "metric_value": 0.25,
                "metric_unit": "ratio",
                "next_action": "search for additional independent sources",
            }],
        })

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more background"})])]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "web_search"
    finally:
        db.close()


def test_delivery_experiment_next_action_blocks_read_only_shell(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a generic deliverable", title="deliverable", kind="generic")
        db.update_job_metadata(job_id, {
            "experiment_ledger": [{
                "title": "deliverable gap",
                "status": "measured",
                "metric_name": "coverage",
                "metric_value": 0.25,
                "metric_unit": "ratio",
                "next_action": "merge the measured output into the deliverable file",
            }],
        })

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "cat output.txt 2>/dev/null"})])]),
            registry=SuccessRegistry(),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "experiment next action pending"
    finally:
        db.close()


def test_delivery_experiment_next_action_allows_write_shell(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a generic deliverable", title="deliverable", kind="generic")
        db.update_job_metadata(job_id, {
            "experiment_ledger": [{
                "title": "deliverable gap",
                "status": "measured",
                "metric_name": "coverage",
                "metric_value": 0.25,
                "metric_unit": "ratio",
                "next_action": "merge the measured output into the deliverable file",
            }],
        })

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": "printf updated > output.txt"})])]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "shell_exec"
    finally:
        db.close()


def test_write_file_can_consume_recent_shell_evidence(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Create a concrete output", title="output", kind="generic")

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
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="write_file", arguments={"path": "out.txt", "content": "done"})])]),
            registry=SuccessRegistry(),
        )

        assert first.tool_name == "shell_exec"
        assert second.status == "completed"
        assert second.tool_name == "write_file"
    finally:
        db.close()


def test_write_file_creates_validation_obligation_for_code_outputs(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Create a validated script", title="validate-file", kind="generic")
        path = tmp_path / "generated.py"

        first = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="write_file",
                    arguments={"path": str(path), "content": "print('ok')\n"},
                )])
            ]),
        )
        job = db.get_job(job_id)
        obligation = job["metadata"]["pending_file_validation_obligation"]

        assert first.status == "completed"
        assert obligation["path"] == str(path)
        assert "py_compile" in obligation["suggested_validation"]
    finally:
        db.close()


def test_file_validation_obligation_blocks_research_until_validated(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Create a validated script", title="validate-file", kind="generic")
        path = tmp_path / "generated.py"
        run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="write_file",
                    arguments={"path": str(path), "content": "print('ok')\n"},
                )])
            ]),
        )

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more context"})])]),
            registry=SuccessRegistry(),
        )
        validated = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": f"python3 -m py_compile {path}"},
                )])
            ]),
        )
        job = db.get_job(job_id)

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "file validation pending"
        assert validated.status == "completed"
        assert job["metadata"].get("pending_file_validation_obligation") == {}
        assert job["metadata"]["last_file_validation_obligation"]["resolution_status"] == "validated"
    finally:
        db.close()


def test_delivery_experiment_next_action_allows_internal_artifact_review(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Improve a generic deliverable", title="deliverable", kind="generic")
        db.update_job_metadata(job_id, {
            "experiment_ledger": [{
                "title": "deliverable gap",
                "status": "measured",
                "metric_name": "coverage",
                "metric_value": 0.25,
                "metric_unit": "ratio",
                "next_action": "merge the measured output into the deliverable file",
            }],
        })

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="search_artifacts", arguments={"query": "saved evidence"})])]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "search_artifacts"
    finally:
        db.close()


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
                    "name": "Custom integration, workflow automation, reliability testing, reporting",
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


def test_fresh_evidence_guard_takes_priority_over_duplicate_read(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Save fresh evidence before reviewing old artifacts", title="fresh-evidence")
        run_id = db.start_run(job_id)
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        artifacts = ArtifactStore(tmp_path, db)
        stored = artifacts.write_text(job_id=job_id, run_id=run_id, step_id=step_id, title="Old Evidence", content="saved")
        db.finish_step(step_id, status="completed", output_data={"success": True, "artifact_id": stored.id, "path": str(stored.path)})
        db.finish_run(run_id, "completed")

        read = ToolCall(name="read_artifact", arguments={"artifact_id": stored.id})
        first_read = run_one_step(job_id, config=config, db=db, llm=ScriptedLLM([LLMResponse(tool_calls=[read])]))
        assert first_read.status == "completed"

        shell = ToolCall(name="shell_exec", arguments={"command": "find . -type f"})
        evidence = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([LLMResponse(tool_calls=[shell])]),
            registry=LargeShellEvidenceRegistry(),
        )
        assert evidence.status == "completed"

        blocked = run_one_step(job_id, config=config, db=db, llm=ScriptedLLM([LLMResponse(tool_calls=[read])]))

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "artifact required before more research"
        assert blocked.result["blocked_tool"] == "read_artifact"
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


def test_run_one_step_blocks_browser_tools_after_runtime_missing(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Browser runtime can be unavailable", title="browser-runtime")
        run_id = db.start_run(job_id)
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="browser_navigate",
            input_data={"arguments": {"url": "https://example.test"}},
        )
        db.finish_step(
            step_id,
            status="failed",
            output_data={
                "success": False,
                "error": "Chrome not found. Checked: Playwright browser cache and Puppeteer browser cache.",
            },
            summary="browser_navigate failed: Chrome not found",
        )
        db.finish_run(run_id, "failed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="browser_snapshot", arguments={"full": False})])
            ]),
            registry=SnapshotRegistry(),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "browser runtime unavailable"
        assert result.result["browser_runtime"]["tool"] == "browser_navigate"
        assert "Use web_search" in result.result["guidance"]
    finally:
        db.close()


def test_run_one_step_allows_non_browser_work_after_runtime_missing(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Use fallback tools when browser is missing", title="browser-runtime")
        run_id = db.start_run(job_id)
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="browser_navigate",
            input_data={"arguments": {"url": "https://example.test"}},
        )
        db.finish_step(
            step_id,
            status="failed",
            output_data={"success": False, "error": "Browser executable doesn't exist on this host."},
            summary="browser_navigate failed: browser executable missing",
        )
        db.finish_run(run_id, "failed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "public docs", "limit": 5})])
            ]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "web_search"
    finally:
        db.close()


def test_run_one_step_skips_batched_browser_call_when_runtime_missing_and_fallback_present(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Use fallback tools when browser is missing", title="browser-runtime")
        run_id = db.start_run(job_id)
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="browser_navigate",
            input_data={"arguments": {"url": "https://example.test"}},
        )
        db.finish_step(
            step_id,
            status="failed",
            output_data={"success": False, "error": "Chrome not found. Checked: Playwright browser cache."},
            summary="browser_navigate failed: Chrome not found",
        )
        db.finish_run(run_id, "failed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="browser_navigate", arguments={"url": "https://example.test/next"}),
                    ToolCall(name="web_search", arguments={"query": "public docs", "limit": 5}),
                ])
            ]),
            registry=SuccessRegistry(),
        )

        tool_steps = [step for step in db.list_steps(job_id=job_id) if step.get("kind") == "tool"]
        assert result.status == "completed"
        assert result.tool_name == "web_search"
        assert tool_steps[-1]["tool_name"] == "web_search"
        assert all(
            step["input"].get("arguments", {}).get("url") != "https://example.test/next"
            for step in tool_steps
            if step.get("tool_name") == "browser_navigate"
        )
    finally:
        db.close()


def test_run_one_step_removes_browser_tools_from_schema_after_runtime_missing(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Use fallback tools when browser is missing", title="browser-runtime")
        run_id = db.start_run(job_id)
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="browser_navigate",
            input_data={"arguments": {"url": "https://example.test"}},
        )
        db.finish_step(
            step_id,
            status="failed",
            output_data={"success": False, "error": "Chrome not found. Checked: Playwright browser cache."},
            summary="browser_navigate failed: Chrome not found",
        )
        db.finish_run(run_id, "failed")
        llm = CapturingLLM(LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "fallback"})]))

        run_one_step(
            job_id,
            config=config,
            db=db,
            llm=llm,
            registry=BrowserAndWebRegistry(),
        )

        tool_names = [tool["function"]["name"] for tool in llm.tools]
        assert tool_names == ["web_search"]
    finally:
        db.close()


def test_run_one_step_removes_browser_tools_after_older_runtime_missing(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Use fallback tools when browser is missing", title="browser-runtime")
        run_id = db.start_run(job_id)
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="browser_navigate",
            input_data={"arguments": {"url": "https://example.test"}},
        )
        db.finish_step(
            step_id,
            status="failed",
            output_data={"success": False, "error": "Chrome not found. Checked: Playwright browser cache."},
            summary="browser_navigate failed: Chrome not found",
        )
        for index in range(80):
            filler_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search")
            db.finish_step(
                filler_id,
                status="completed",
                output_data={"success": True, "query": f"query {index}", "results": []},
                summary=f"web_search query {index}",
            )
        db.finish_run(run_id, "completed")
        llm = CapturingLLM(LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "fallback"})]))

        run_one_step(
            job_id,
            config=config,
            db=db,
            llm=llm,
            registry=BrowserAndWebRegistry(),
        )

        tool_names = [tool["function"]["name"] for tool in llm.tools]
        assert tool_names == ["web_search"]
    finally:
        db.close()


def test_run_one_step_allows_repeated_defer_for_monitor_intervals(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    call = ToolCall(name="defer_job", arguments={"seconds": 60, "reason": "wait for monitor interval"})
    try:
        job_id = db.create_job("Check a long-running process later", title="defer")
        first = run_one_step(job_id, config=config, db=db, llm=ScriptedLLM([LLMResponse(tool_calls=[call])]))
        second = run_one_step(job_id, config=config, db=db, llm=ScriptedLLM([LLMResponse(tool_calls=[call])]))

        assert first.status == "completed"
        assert second.status == "completed"
        assert first.tool_name == "defer_job"
        assert second.tool_name == "defer_job"
    finally:
        db.close()


def test_run_one_step_blocks_self_defer_for_next_worker_turn(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep making progress", title="self-defer")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(
                        name="defer_job",
                        arguments={
                            "seconds": 300,
                            "reason": "waiting for tasks to be picked up by next worker turn",
                            "next_action": "continue in the next worker step",
                        },
                    )
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.tool_name == "defer_job"
        assert result.result["error"] == "self-defer blocked"
        assert result.result["self_defer"]["matched"] == "next worker turn"
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
        assert next_result.status == "blocked"
        assert next_result.result["error"] == "evidence checkpoint accounting required"
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


def test_prompt_pushes_deliverable_checkpoint_after_long_research():
    job = {
        "title": "paper",
        "kind": "generic",
        "objective": "write a complete research paper from evidence",
        "metadata": {
            "task_queue": [
                {
                    "title": "Save the first durable draft",
                    "status": "open",
                    "priority": 8,
                    "output_contract": "report",
                }
            ],
        },
    }
    steps = [
        {
            "step_no": index + 1,
            "status": "completed",
            "kind": "tool",
            "tool_name": "shell_exec",
            "input": {"arguments": {"command": f"cat source_{index}.txt"}},
        }
        for index in range(18)
    ]

    content = build_messages(job, steps)[-1]["content"]

    assert "Deliverable progress guard:" in content
    assert "durable deliverable checkpoint" in content
    assert "write_file or write_artifact" in content


def test_low_priority_report_task_does_not_block_execution_task_prompt():
    job = {
        "title": "execution",
        "kind": "generic",
        "objective": "keep useful work moving",
        "metadata": {
            "task_queue": [
                {
                    "title": "Review saved output later",
                    "status": "open",
                    "priority": 4,
                    "output_contract": "report",
                },
                {
                    "title": "Run current experiment",
                    "status": "active",
                    "priority": 9,
                    "output_contract": "experiment",
                },
            ],
        },
    }
    steps = [
        {
            "step_no": index + 1,
            "status": "completed",
            "kind": "tool",
            "tool_name": "shell_exec",
            "input": {"arguments": {"command": f"probe_{index}"}},
        }
        for index in range(18)
    ]

    content = build_messages(job, steps)[-1]["content"]

    assert "Deliverable progress guard:\nNone." in content
    assert "durable deliverable checkpoint" not in content


def test_run_one_step_blocks_more_research_when_deliverable_needs_checkpoint(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Write a complete report from collected evidence",
            title="deliverable",
            metadata={
                "task_queue": [
                    {
                        "title": "Save the first durable report checkpoint",
                        "status": "open",
                        "priority": 8,
                        "output_contract": "report",
                    }
                ]
            },
        )
        run_id = db.start_run(job_id, model="fake")
        for index in range(15):
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="shell_exec",
                input_data={"arguments": {"command": f"cat source_{index}.txt"}},
            )
            db.finish_step(step_id, status="completed", output_data={"success": True, "stdout": "note"})
        ledger_step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_findings")
        db.finish_step(ledger_step_id, status="completed", output_data={"success": True})
        for index in range(15, 18):
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="tool",
                tool_name="shell_exec",
                input_data={"arguments": {"command": f"cat source_{index}.txt"}},
            )
            db.finish_step(step_id, status="completed", output_data={"success": True, "stdout": "note"})
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "more background sources"})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "deliverable checkpoint required"
        assert result.result["blocked_tool"] == "web_search"
        assert result.result["recoverable"] is True
    finally:
        db.close()


def test_prompt_includes_roadmap_and_validation_constraints():
    job = {
        "title": "broad work",
        "kind": "generic",
        "objective": "build a broad durable outcome",
        "metadata": {
            "roadmap": {
                "title": "Broad Roadmap",
                "status": "active",
                "current_milestone": "Foundation",
                "validation_contract": "check observable evidence",
                "milestones": [{
                    "title": "Foundation",
                    "status": "validating",
                    "validation_status": "pending",
                    "acceptance_criteria": "evidence exists",
                    "evidence_needed": "saved output",
                    "features": [{"title": "First feature", "status": "done"}],
                }],
            },
        },
    }

    messages = build_messages(job, [])
    content = messages[-1]["content"]

    assert "Roadmap:" in content
    assert "Broad Roadmap" in content
    assert "validation=pending" in content
    assert "Use record_milestone_validation" in content


def test_prompt_suggests_roadmap_for_broad_jobs_without_one():
    job = {
        "title": "broad work",
        "kind": "generic",
        "objective": "research and implement a broad multi phase system with validation and durable output",
        "metadata": {},
    }

    messages = build_messages(job, [])
    content = messages[-1]["content"]

    assert "No roadmap yet" in content
    assert "use record_roadmap" in content


def test_run_one_step_blocks_branch_work_when_milestone_needs_validation(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Keep broad work gated by validation",
            title="roadmap-gate",
            metadata={
                "roadmap": {
                    "title": "Generic Roadmap",
                    "status": "active",
                    "milestones": [{
                        "title": "Foundation",
                        "status": "validating",
                        "validation_status": "pending",
                        "acceptance_criteria": "evidence exists",
                        "evidence_needed": "saved artifact",
                    }],
                },
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "new branch", "limit": 5})])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "milestone validation required"
        assert result.result["blocked_tool"] == "web_search"
    finally:
        db.close()


def test_run_one_step_allows_milestone_validation_when_gate_is_active(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Validate a gated milestone",
            title="roadmap-validate",
            metadata={
                "roadmap": {
                    "title": "Generic Roadmap",
                    "status": "active",
                    "milestones": [{
                        "title": "Foundation",
                        "status": "validating",
                        "validation_status": "pending",
                    }],
                },
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="record_milestone_validation", arguments={
                    "milestone": "Foundation",
                    "validation_status": "passed",
                    "result": "Acceptance criteria met.",
                    "evidence": "artifact",
                })])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_milestone_validation"
        roadmap = db.get_job(job_id)["metadata"]["roadmap"]
        assert roadmap["milestones"][0]["validation_status"] == "passed"
    finally:
        db.close()


def test_run_one_step_blocks_task_churn_when_roadmap_stalls(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Keep roadmap aligned with broad work",
            title="roadmap-stale",
            metadata={
                "roadmap": {
                    "title": "Generic Roadmap",
                    "status": "planned",
                    "milestones": [{
                        "title": "Foundation",
                        "status": "planned",
                        "validation_status": "not_started",
                    }],
                },
                "task_queue": [{"title": f"Task {index}", "status": "done"} for index in range(8)],
            },
        )
        run_id = db.start_run(job_id, model="fake")
        for index in range(2):
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
            db.finish_step(step_id, status="completed", summary=f"artifact {index}", output_data={"success": True})

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="record_tasks", arguments={
                    "tasks": [{"title": "More task churn", "status": "open"}]
                })])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "roadmap update required"
        assert result.result["blocked_tool"] == "record_tasks"
    finally:
        db.close()


def test_run_one_step_allows_roadmap_update_when_roadmap_stalls(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Update stale roadmap",
            title="roadmap-update",
            metadata={
                "roadmap": {
                    "title": "Generic Roadmap",
                    "status": "planned",
                    "milestones": [{
                        "title": "Foundation",
                        "status": "planned",
                        "validation_status": "not_started",
                    }],
                },
                "task_queue": [{"title": f"Task {index}", "status": "done"} for index in range(8)],
            },
        )
        run_id = db.start_run(job_id, model="fake")
        for index in range(2):
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
            db.finish_step(step_id, status="completed", summary=f"artifact {index}", output_data={"success": True})

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="record_roadmap", arguments={
                    "title": "Generic Roadmap",
                    "status": "active",
                    "current_milestone": "Foundation",
                    "milestones": [{
                        "title": "Foundation",
                        "status": "active",
                        "validation_status": "pending",
                        "acceptance_criteria": "evidence reviewed",
                    }],
                })])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_roadmap"
        roadmap = db.get_job(job_id)["metadata"]["roadmap"]
        assert roadmap["status"] == "active"
        assert roadmap["milestones"][0]["validation_status"] == "pending"
    finally:
        db.close()


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


def test_run_one_step_blocks_new_tasks_when_queue_is_saturated(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Finish existing work",
            title="saturated",
            kind="generic",
            metadata={
                "task_queue": [
                    {"title": f"Open branch {index}", "status": "open", "priority": index}
                    for index in range(40)
                ]
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_tasks", arguments={"tasks": [{"title": "Yet another branch", "status": "open"}]})
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "task queue saturated"
        assert result.result["task_queue"]["open_count"] == 40
    finally:
        db.close()


def test_run_one_step_ignores_guard_recovery_tasks_for_queue_saturation(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Continue objective work after guard recovery",
            title="guard-task-sprawl",
            kind="generic",
            metadata={
                "task_queue": [
                    {
                        "title": f"Resolve guard: recoverable blocker {index}",
                        "status": "open",
                        "priority": 9,
                        "metadata": {"guard_recovery": {"error": f"recoverable blocker {index}"}},
                    }
                    for index in range(45)
                ]
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_tasks", arguments={"tasks": [{"title": "Run next objective branch", "status": "open"}]})
                ])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_tasks"
        job = db.get_job(job_id)
        assert any(task["title"] == "Run next objective branch" for task in job["metadata"]["task_queue"])
    finally:
        db.close()


def test_run_one_step_ignores_guard_recovery_tasks_for_total_sprawl(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Continue objective work after many recovered guards",
            title="guard-total-sprawl",
            kind="generic",
            metadata={
                "task_queue": [
                    {
                        "title": f"Resolve guard: recovered blocker {index}",
                        "status": "done",
                        "priority": 9,
                        "metadata": {"guard_recovery": {"error": f"recovered blocker {index}"}},
                    }
                    for index in range(85)
                ]
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_tasks", arguments={"tasks": [{"title": "Fresh objective branch", "status": "open"}]})
                ])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_tasks"
        job = db.get_job(job_id)
        assert any(task["title"] == "Fresh objective branch" for task in job["metadata"]["task_queue"])
    finally:
        db.close()


def test_run_one_step_blocks_read_only_shell_churn(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Choose from discovered candidates", title="read-only-churn", kind="generic")
        for command in [
            "find /tmp/work -type f | head",
            "ls -lah /tmp/work",
            "curl -s https://example.test/api/list | head -100",
        ]:
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec", input_data={"arguments": {"command": command}})
            db.finish_step(step_id, status="completed", output_data={"success": True, "returncode": 0, "stdout": "candidate-a\ncandidate-b"})
            db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="shell_exec", arguments={"command": "curl -s https://example.test/api/list?page=2"})
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "action decision required"
        assert result.result["read_only_shell_churn"]["read_only_shell_count"] == 3
    finally:
        db.close()


def test_run_one_step_allows_action_after_read_only_shell_churn(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Act after discovered candidates", title="read-only-to-action", kind="generic")
        for command in [
            "find /tmp/work -type f | head",
            "ls -lah /tmp/work",
            "curl -s https://example.test/api/list | head -100",
        ]:
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec", input_data={"arguments": {"command": command}})
            db.finish_step(step_id, status="completed", output_data={"success": True, "returncode": 0, "stdout": "candidate-a\ncandidate-b"})
            db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="shell_exec", arguments={"command": "python run_candidate.py --input candidate-a"})
                ])
            ]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "shell_exec"
    finally:
        db.close()


def test_run_one_step_allows_read_only_shell_after_durable_decision(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Recover from inspection churn", title="read-only-decision", kind="generic")
        for command in [
            "find /tmp/work -type f | head",
            "ls -lah /tmp/work",
            "curl -s https://example.test/api/list | head -100",
        ]:
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec", input_data={"arguments": {"command": command}})
            db.finish_step(step_id, status="completed", output_data={"success": True, "returncode": 0, "stdout": "candidate-a\ncandidate-b"})
            db.finish_run(run_id, "completed")

        run_id = db.start_run(job_id, model="test")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="record_lesson")
        db.finish_step(
            step_id,
            status="completed",
            output_data={"success": True, "lesson": {"category": "decision", "lesson": "Use candidate-a and inspect its exact metadata next."}},
        )
        db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="shell_exec", arguments={"command": "ls -lah /tmp/work/candidate-a"})
                ])
            ]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "shell_exec"
    finally:
        db.close()


def test_run_one_step_allows_explicit_download_after_read_only_shell_churn(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Download selected candidate", title="read-only-to-download", kind="generic")
        for command in [
            "find /tmp/work -type f | head",
            "ls -lah /tmp/work",
            "curl -s https://example.test/api/list | head -100",
        ]:
            run_id = db.start_run(job_id, model="test")
            step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="shell_exec", input_data={"arguments": {"command": command}})
            db.finish_step(step_id, status="completed", output_data={"success": True, "returncode": 0, "stdout": "candidate-a\ncandidate-b"})
            db.finish_run(run_id, "completed")

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="shell_exec", arguments={"command": "curl -L -o /tmp/candidate.bin https://example.test/candidate.bin"})
                ])
            ]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "shell_exec"
    finally:
        db.close()


def test_run_one_step_blocks_new_tasks_when_queue_sprawls(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Consolidate long-running work",
            title="task-sprawl",
            kind="generic",
            metadata={
                "task_queue": [
                    {"title": f"Completed branch {index}", "status": "done", "priority": index}
                    for index in range(80)
                ]
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_tasks", arguments={"tasks": [{"title": "New branch", "status": "open"}]})
                ])
            ]),
        )

        assert result.status == "blocked"
        assert result.result["error"] == "task queue saturated"
        assert result.result["task_queue"]["reason"] == "total task queue is too large"
        assert result.result["task_queue"]["total_count"] == 80
    finally:
        db.close()


def test_recent_task_saturation_keeps_record_tasks_for_existing_updates(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Execute existing work",
            title="saturated-tools",
            kind="generic",
            metadata={
                "task_queue": [
                    {"title": f"Open branch {index}", "status": "open", "priority": index}
                    for index in range(40)
                ]
            },
        )
        first = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_tasks", arguments={"tasks": [{"title": "New branch", "status": "open"}]})
                ])
            ]),
        )
        assert first.status == "blocked"
        llm = CapturingLLM(LLMResponse(tool_calls=[ToolCall(name="record_lesson", arguments={"lesson": "execute existing work"})]))

        run_one_step(job_id, config=config, db=db, llm=llm)

        tool_names = {tool["function"]["name"] for tool in llm.tools}
        prompt = llm.messages[-1]["content"]
        assert "Task queue saturation" in prompt
        assert "Do not create new task branches" in prompt
        assert "record_tasks only to update existing task titles" in prompt
        assert "record_tasks" in tool_names
        assert "record_lesson" in tool_names
        assert "shell_exec" in tool_names
    finally:
        db.close()


def test_run_one_step_allows_existing_task_update_when_queue_is_saturated(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job(
            "Finish existing work",
            title="saturated",
            kind="generic",
            metadata={
                "task_queue": [
                    {"title": f"Open branch {index}", "status": "open", "priority": index}
                    for index in range(40)
                ]
            },
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(name="record_tasks", arguments={"tasks": [{"title": "Open branch 0", "status": "active"}]})
                ])
            ]),
        )

        assert result.status == "completed"
        assert result.tool_name == "record_tasks"
        job = db.get_job(job_id)
        assert job["metadata"]["task_queue"][0]["status"] == "active"
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


def test_run_one_step_allows_child_url_when_bad_web_source_is_domain_root(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid over-broad domain source blocks", title="guard")
        db.append_source_record(
            job_id,
            "https://source.example",
            source_type="web_source",
            usefulness_score=0.05,
            fail_count_delta=1,
            outcome="root health check failed",
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="web_extract",
                    arguments={"urls": ["https://source.example/api/public/models"]},
                )])
            ]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "web_extract"
    finally:
        db.close()


def test_run_one_step_records_failed_shell_url_source(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid broken shell URL sources", title="guard")
        url = "https://source.example/api/private/tree/main"

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(name="shell_exec", arguments={"command": f"curl -s {url}"})])
            ]),
            registry=FailedUrlShellRegistry(),
        )
        sources = db.get_job(job_id)["metadata"]["source_ledger"]
        source = sources[0]

        assert result.status == "failed"
        assert source["source"] == url
        assert source["source_type"] == "shell_exec"
        assert source["fail_count"] == 1
        assert source["usefulness_score"] == 0.01
        assert source["metadata"]["failure_kind"] == "auth_or_http"
    finally:
        db.close()


def test_run_one_step_records_pathful_failed_shell_urls_not_root_health_checks(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid poisoning whole hosts from mixed probes", title="guard")
        bad_url = "https://source.example/api/private/tree/main"

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": f"curl -sI https://source.example && curl -s {bad_url}"},
                )])
            ]),
            registry=FailedUrlShellRegistry(),
        )
        sources = db.get_job(job_id)["metadata"]["source_ledger"]

        assert result.status == "failed"
        assert [source["source"] for source in sources] == [bad_url]
    finally:
        db.close()


def test_run_one_step_blocks_known_bad_shell_source_family(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Pivot from failed source family", title="guard")

        first = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -L https://source.example/downloads/private/model-a.bin"},
                )])
            ]),
            registry=FailedUrlShellRegistry(),
        )
        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -L https://source.example/downloads/private/model-b.bin"},
                )])
            ]),
        )
        allowed = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -L https://source.example/downloads/public/model-b.bin"},
                )])
            ]),
            registry=SuccessRegistry(),
        )

        assert first.status == "failed"
        assert blocked.status == "blocked"
        assert blocked.result["error"] == "known bad source blocked"
        assert blocked.result["known_bad_source"]["source"] == "https://source.example/downloads/private"
        assert allowed.status == "completed"
    finally:
        db.close()


def test_run_one_step_derives_bad_shell_source_family_from_exact_failure(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Pivot from exact failed file source", title="guard")
        db.append_source_record(
            job_id,
            "https://source.example/downloads/private/model-a.bin",
            source_type="shell_exec",
            usefulness_score=0.01,
            fail_count_delta=1,
            warnings=["auth failure"],
            outcome="401 Unauthorized",
        )

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -L https://source.example/downloads/private/model-b.bin"},
                )])
            ]),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "known bad source blocked"
        assert blocked.result["known_bad_source"]["source"] == "https://source.example/downloads/private"
        assert blocked.result["known_bad_source"]["metadata"]["source_family_from"].endswith("/model-a.bin")
    finally:
        db.close()


def test_run_one_step_does_not_block_entire_host_after_auth_source_families(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Pivot from repeated host auth failures", title="guard")
        for source in (
            "https://source.example/private/a/model.bin",
            "https://source.example/private/b/model.bin",
            "https://source.example/private/c/model.bin",
        ):
            db.append_source_record(
                job_id,
                source,
                source_type="shell_exec",
                usefulness_score=0.01,
                fail_count_delta=1,
                warnings=["401 unauthorized"],
                outcome="HTTP 401 Unauthorized",
                metadata={"failure_kind": "auth_or_http"},
            )

        allowed_same_host = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -L https://source.example/private/d/model.bin"},
                )])
            ]),
            registry=SuccessRegistry(),
        )
        allowed = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -L https://other.example/private/d/model.bin"},
                )])
            ]),
            registry=SuccessRegistry(),
        )

        assert allowed_same_host.status == "completed"
        assert allowed.status == "completed"
    finally:
        db.close()


def test_run_one_step_blocks_known_bad_shell_source_path(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Pivot from failed shell URL source", title="guard")
        db.append_source_record(
            job_id,
            "https://source.example/api/private/tree/main",
            source_type="shell_exec",
            usefulness_score=0.01,
            fail_count_delta=1,
            warnings=["auth failure"],
            outcome="401 Unauthorized",
        )

        blocked = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -s 'https://source.example/api/private/tree/main?recursive=true'"},
                )])
            ]),
        )
        allowed = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -s 'https://source.example/api/public/models'"},
                )])
            ]),
            registry=SuccessRegistry(),
        )

        assert blocked.status == "blocked"
        assert blocked.result["error"] == "known bad source blocked"
        assert blocked.result["known_bad_source"]["source"] == "https://source.example/api/private/tree/main"
        assert allowed.status == "completed"
    finally:
        db.close()


def test_run_one_step_allows_mixed_shell_command_with_bad_root_health_check(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Avoid over-broad shell root source blocks", title="guard")
        db.append_source_record(
            job_id,
            "https://source.example",
            source_type="shell_exec",
            usefulness_score=0.01,
            fail_count_delta=1,
            warnings=["root health check failed earlier"],
            outcome="HTTP failure",
        )

        result = run_one_step(
            job_id,
            config=config,
            db=db,
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="shell_exec",
                    arguments={"command": "curl -sI https://source.example && curl -s https://source.example/api/public/models"},
                )])
            ]),
            registry=SuccessRegistry(),
        )

        assert result.status == "completed"
        assert result.tool_name == "shell_exec"
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
