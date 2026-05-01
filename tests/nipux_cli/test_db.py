from nipux_cli.db import AgentDB


def test_db_job_run_step_and_artifact_roundtrip(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic every day", title="research", kind="generic")
        assert job_id == "research"
        job = db.get_job(job_id)
        assert job["status"] == "queued"
        assert job["kind"] == "generic"

        run_id = db.start_run(job_id, model="local-test-model")
        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="tool",
            tool_name="write_artifact",
            input_data={"x": 1},
        )
        db.finish_step(step_id, status="completed", summary="wrote artifact", output_data={"ok": True})
        artifact_id = db.add_artifact(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            path=tmp_path / "artifact.md",
            sha256="abc",
            artifact_type="text",
            title="A",
        )
        db.finish_run(run_id, "completed")

        assert db.get_job(job_id)["status"] == "running"
        assert db.list_steps(run_id=run_id)[0]["output"]["ok"] is True
        assert db.list_runs(job_id)[0]["id"] == run_id
        assert db.get_artifact(artifact_id)["title"] == "A"
        assert db.list_artifacts(job_id)[0]["id"] == artifact_id
    finally:
        db.close()


def test_create_job_uses_unique_readable_slug_ids(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        first = db.create_job("Research topic", title="Nightly Research")
        second = db.create_job("Research more topics", title="Nightly Research")

        assert first == "nightly-research"
        assert second == "nightly-research-2"
    finally:
        db.close()


def test_step_numbers_increment_across_runs_for_a_job(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Long job")
        run_1 = db.start_run(job_id, model="fake")
        step_1 = db.add_step(job_id=job_id, run_id=run_1, kind="tool")
        db.finish_step(step_1, status="completed")
        db.finish_run(run_1, "completed")

        run_2 = db.start_run(job_id, model="fake")
        step_2 = db.add_step(job_id=job_id, run_id=run_2, kind="tool")

        steps = db.list_steps(job_id=job_id)
        assert step_2 != step_1
        assert [step["step_no"] for step in steps] == [1, 2]
    finally:
        db.close()


def test_job_token_usage_aggregates_message_usage(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Long job")
        db.append_event(
            job_id,
            event_type="loop",
            title="message_end",
            metadata={
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                    "cost": 0.001,
                    "prompt_tokens_details": {"cached_tokens": 10},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                }
            },
        )
        db.append_event(
            job_id,
            event_type="loop",
            title="message_end",
            metadata={"usage": {"prompt_tokens": 150, "completion_tokens": 50, "total_tokens": 200, "estimated": True}},
        )

        usage = db.job_token_usage(job_id)

        assert usage["prompt_tokens"] == 250
        assert usage["completion_tokens"] == 75
        assert usage["total_tokens"] == 325
        assert usage["latest_prompt_tokens"] == 150
        assert usage["cost"] == 0.001
        assert usage["has_cost"] is True
        assert usage["estimated_calls"] == 1
        assert usage["reasoning_tokens"] == 3
        assert usage["cached_tokens"] == 10
    finally:
        db.close()


def test_append_operator_message_roundtrip(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")

        entry = db.append_operator_message(job_id, "Focus on artifact-backed findings", source="shell")
        job = db.get_job(job_id)

        assert entry["message"] == "Focus on artifact-backed findings"
        assert job["metadata"]["operator_messages"][0]["source"] == "shell"
        assert job["metadata"]["operator_messages"][0]["mode"] == "steer"
        assert job["metadata"]["operator_messages"][0]["message"] == "Focus on artifact-backed findings"
        assert job["metadata"]["last_operator_message"]["message"] == "Focus on artifact-backed findings"
        events = db.list_timeline_events(job_id)
        assert events[-1]["event_type"] == "operator_message"
        assert events[-1]["body"] == "Focus on artifact-backed findings"
    finally:
        db.close()


def test_claim_operator_messages_marks_one_message_at_a_time(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        first = db.append_operator_message(job_id, "first steer", source="chat")
        db.append_operator_message(job_id, "second steer", source="chat")

        claimed = db.claim_operator_messages(job_id, modes=("steer",), limit=1)
        second_claim = db.claim_operator_messages(job_id, modes=("steer",), limit=1)

        job = db.get_job(job_id)
        messages = job["metadata"]["operator_messages"]
        events = db.list_timeline_events(job_id, limit=20)

        assert [item["message"] for item in claimed] == ["first steer"]
        assert claimed[0]["event_id"] == first["event_id"]
        assert [item["message"] for item in second_claim] == ["second steer"]
        assert all(message.get("claimed_at") for message in messages)
        assert any(event["event_type"] == "loop" and event["title"] == "steering claimed" for event in events)
    finally:
        db.close()


def test_acknowledge_operator_messages_marks_delivered_context(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")
        entry = db.append_operator_message(job_id, "correct the target before continuing", source="chat")
        db.claim_operator_messages(job_id, modes=("steer",), limit=1)

        result = db.acknowledge_operator_messages(
            job_id,
            message_ids=[entry["event_id"]],
            summary="target correction incorporated",
        )

        job = db.get_job(job_id)
        message = job["metadata"]["operator_messages"][0]
        events = db.list_timeline_events(job_id, limit=20)

        assert result["count"] == 1
        assert message["acknowledged_at"]
        assert job["metadata"]["last_operator_context_ack"]["summary"] == "target correction incorporated"
        assert any(event["event_type"] == "operator_context" for event in events)
    finally:
        db.close()


def test_rename_job_updates_title_without_changing_id(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="old title")

        renamed = db.rename_job(job_id, "new title")
        job = db.get_job(job_id)

        assert renamed["id"] == job_id
        assert renamed["title"] == "new title"
        assert job["title"] == "new title"
    finally:
        db.close()


def test_delete_job_removes_related_rows(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="delete me")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        artifact_path = tmp_path / "artifact.md"
        artifact_path.write_text("artifact", encoding="utf-8")
        db.add_artifact(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            path=artifact_path,
            sha256="abc",
            artifact_type="text",
            title="Artifact",
        )
        db.upsert_memory(job_id=job_id, key="rolling_state", summary="summary")

        result = db.delete_job(job_id)

        assert result["job"]["title"] == "delete me"
        assert result["counts"]["runs"] == 1
        assert result["counts"]["steps"] == 1
        assert result["counts"]["artifacts"] == 1
        assert result["counts"]["memory"] == 1
        try:
            db.get_job(job_id)
        except KeyError:
            pass
        else:
            raise AssertionError("job still exists after delete")
        assert db.list_steps(job_id=job_id) == []
        assert db.list_artifacts(job_id) == []
        assert db.list_memory(job_id) == []
    finally:
        db.close()


def test_append_lesson_roundtrip(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")

        entry = db.append_lesson(
            job_id,
            "Low-evidence pages are not useful evidence sources.",
            category="source_quality",
            confidence=0.9,
        )
        job = db.get_job(job_id)

        assert entry["category"] == "source_quality"
        assert job["metadata"]["lessons"][0]["lesson"] == "Low-evidence pages are not useful evidence sources."
        assert job["metadata"]["last_lesson"]["confidence"] == 0.9
    finally:
        db.close()


def test_append_lesson_dedupes_repeated_memory(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")

        first = db.append_lesson(job_id, "Use chamber directories.", category="strategy", metadata={"step": 1})
        second = db.append_lesson(job_id, "Use chamber directories.", category="strategy", metadata={"step": 2})
        job = db.get_job(job_id)

        assert first["lesson"] == second["lesson"]
        assert len(job["metadata"]["lessons"]) == 1
        assert job["metadata"]["lessons"][0]["seen_count"] == 2
        assert job["metadata"]["lessons"][0]["metadata"]["step"] == 2
    finally:
        db.close()


def test_source_and_finding_ledgers_dedupe_and_update(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")

        source = db.append_source_record(
            job_id,
            "https://example.com/source",
            source_type="web_source",
            usefulness_score=0.7,
            yield_count=3,
            outcome="yielded reusable findings",
        )
        updated_source = db.append_source_record(
            job_id,
            "https://example.com/source",
            usefulness_score=0.9,
            yield_count=2,
            fail_count_delta=1,
        )
        finding = db.append_finding_record(
            job_id,
            name="Acme Finding",
            url="https://acme.example",
            category="example category",
            score=0.8,
        )
        updated_finding = db.append_finding_record(
            job_id,
            name="Acme Finding",
            url="https://acme.example",
            contact="source note",
            score=0.85,
        )
        reflection = db.append_reflection(job_id, "Keep using directories", strategy="Prioritize chambers")
        job = db.get_job(job_id)

        assert source["key"] == updated_source["key"]
        assert updated_source["yield_count"] == 5
        assert updated_source["fail_count"] == 1
        assert finding["created"] is True
        assert updated_finding["created"] is False
        assert updated_finding["contact"] == "source note"
        assert len(job["metadata"]["source_ledger"]) == 1
        assert len(job["metadata"]["finding_ledger"]) == 1
        assert job["metadata"]["last_reflection"]["summary"] == reflection["summary"]
    finally:
        db.close()


def test_task_queue_dedupes_and_updates(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic")

        first = db.append_task_record(
            job_id,
            title="Explore primary sources",
            status="open",
            priority=3,
            goal="Find direct evidence",
        )
        second = db.append_task_record(
            job_id,
            title="Explore primary sources",
            status="done",
            priority=5,
            result="Saved source artifact",
        )
        job = db.get_job(job_id)

        assert first["created"] is True
        assert second["created"] is False
        assert len(job["metadata"]["task_queue"]) == 1
        assert job["metadata"]["task_queue"][0]["status"] == "done"
        assert job["metadata"]["task_queue"][0]["priority"] == 5
        assert job["metadata"]["task_queue"][0]["result"] == "Saved source artifact"
    finally:
        db.close()


def test_timeline_events_cover_visible_activity(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
        db.append_operator_message(job_id, "operator note", source="test")
        db.append_agent_update(job_id, "agent note", category="chat")
        db.append_lesson(job_id, "durable lesson", category="strategy")
        db.append_source_record(job_id, "https://example.com", usefulness_score=0.7, outcome="useful")
        db.append_finding_record(job_id, name="Reusable finding", reason="evidence")
        db.append_task_record(job_id, title="Explore branch", status="open")
        db.append_reflection(job_id, "reflect summary", strategy="next strategy")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search", input_data={"query": "x"})
        db.finish_step(step_id, status="completed", summary="searched", output_data={"ok": True})
        db.add_artifact(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            path=tmp_path / "artifact.md",
            sha256="abc",
            artifact_type="text",
            title="Artifact",
            summary="saved",
        )
        db.upsert_memory(job_id=job_id, key="rolling_state", summary="compact state")

        events = db.list_timeline_events(job_id, limit=50)
        event_types = {event["event_type"] for event in events}

        assert "operator_message" in event_types
        assert "agent_message" in event_types
        assert "lesson" in event_types
        assert "source" in event_types
        assert "finding" in event_types
        assert "task" in event_types
        assert "reflection" in event_types
        assert "tool_call" in event_types
        assert "tool_result" in event_types
        assert "artifact" in event_types
        assert "compaction" in event_types
        assert any(event["body"] == "operator note" for event in events)
    finally:
        db.close()
