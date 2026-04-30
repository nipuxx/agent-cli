from nipux_cli.operator_context import inactive_prompt_operator_ids, operator_entry_is_prompt_relevant


def _entry(message: str, *, event_id: str = "op_1", mode: str = "steer") -> dict:
    return {"event_id": event_id, "mode": mode, "message": message}


def test_conversation_only_operator_messages_do_not_enter_worker_prompt():
    for message in ("hello", "how is it going?", "clear", "stop 1", "jobs"):
        assert not operator_entry_is_prompt_relevant(_entry(message))


def test_actionable_operator_messages_remain_worker_constraints():
    for message in (
        "do not run local testing on my computer",
        "use the corrected target from the chat",
        "focus on measured results instead of saved notes",
        "the address is wrong, use `target-box`",
    ):
        assert operator_entry_is_prompt_relevant(_entry(message))


def test_inactive_prompt_operator_ids_returns_only_conversation_active_messages():
    messages = [
        _entry("hello", event_id="op_chat"),
        _entry("use the corrected target", event_id="op_use"),
        {**_entry("clear", event_id="op_done"), "acknowledged_at": "2026-04-26T00:00:00+00:00"},
    ]

    assert inactive_prompt_operator_ids(messages) == ["op_chat"]
