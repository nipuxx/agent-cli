"""Chat-controller behavior shared by the interactive CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from nipux_cli.chat_intent import (
    chat_control_command,
    extract_job_objective_from_message,
    message_requests_immediate_run,
    message_requests_queued_job,
)


@dataclass(frozen=True)
class ChatControllerDeps:
    db_factory: Callable[[], tuple[Any, Any]]
    reply_fn: Callable[[str, str], Any]
    create_job: Callable[..., tuple[str, str]]
    write_shell_state: Callable[[dict[str, Any]], None]
    start_daemon: Callable[..., None]
    capture_command: Callable[[str, str], tuple[bool, str]]
    compact_command_output: Callable[[str], list[str]]
    friendly_error_text: Callable[[str], str]


def handle_chat_message(
    job_id: str,
    line: str,
    *,
    deps: ChatControllerDeps,
    reply_fn: Callable[[str, str], Any] | None = None,
    quiet: bool = False,
) -> tuple[bool, str]:
    reply_callable = reply_fn or deps.reply_fn
    spawned = maybe_spawn_job_from_chat(job_id, line, deps=deps, quiet=quiet)
    if spawned:
        return True, spawned
    controlled = handle_chat_control_intent(job_id, line, deps=deps, quiet=quiet)
    if controlled is not None:
        return controlled
    queue_chat_note(job_id, line, deps=deps, mode="steer", quiet=quiet)
    try:
        reply = reply_callable(job_id, line)
    except Exception as exc:
        detail = deps.friendly_error_text(f"{type(exc).__name__}: {exc}")
        message = f"{detail}; message saved for the worker"
        if not quiet:
            print(detail)
            print("Your message is still saved for the next worker step.")
        return True, message
    reply_text, reply_metadata = chat_reply_text_and_metadata(reply)
    if reply_text.strip():
        db, _config = deps.db_factory()
        try:
            if reply_metadata:
                db.append_event(
                    job_id,
                    event_type="loop",
                    title="message_end",
                    body=reply_text[:1000],
                    metadata={"source": "chat", "tool_calls": [], **reply_metadata},
                )
            db.append_agent_update(job_id, reply_text.strip(), category="chat")
        finally:
            db.close()
        if not quiet:
            print()
            print(reply_text.strip())
            print()
        return True, ""
    message = "model returned an empty reply; message is queued"
    if not quiet:
        print("model returned an empty reply; your message is still queued.")
    return True, message


def chat_reply_text_and_metadata(reply: Any) -> tuple[str, dict[str, Any]]:
    content = getattr(reply, "content", None)
    if content is None:
        return str(reply), {}
    metadata: dict[str, Any] = {}
    usage = getattr(reply, "usage", None)
    if isinstance(usage, dict) and usage:
        metadata["usage"] = usage
    model = getattr(reply, "model", "")
    if model:
        metadata["model"] = model
    response_id = getattr(reply, "response_id", "")
    if response_id:
        metadata["response_id"] = response_id
    return str(content), metadata


def handle_chat_control_intent(
    job_id: str,
    line: str,
    *,
    deps: ChatControllerDeps,
    quiet: bool = False,
) -> tuple[bool, str] | None:
    command = chat_control_command(line)
    if not command:
        return None
    keep_running, output = deps.capture_command(job_id, command)
    compact = deps.compact_command_output(output)
    message = " | ".join(compact[-4:]) if compact else f"{command.lstrip('/')} done"
    if not quiet:
        print(message)
    return keep_running, message


def maybe_spawn_job_from_chat(
    job_id: str,
    message: str,
    *,
    deps: ChatControllerDeps,
    quiet: bool = False,
) -> str:
    objective = extract_job_objective_from_message(message)
    if not objective:
        return ""
    created_id, title = deps.create_job(objective=objective, title=None, kind="generic", cadence=None)
    deps.write_shell_state({"focus_job_id": created_id})
    db, _config = deps.db_factory()
    try:
        db.append_operator_message(created_id, message, source="chat", mode="steer")
        run_now = not message_requests_queued_job(message) or message_requests_immediate_run(message)
        update = "Created this job from chat and drafted its initial plan."
        if run_now:
            update += " Starting the daemon so it can begin work."
        else:
            update += " Use the right pane to run it."
        db.append_agent_update(created_id, update, category="chat")
        db.append_agent_update(
            job_id,
            f"Created job '{title}' from your chat request and switched focus to it.",
            category="chat",
        )
    finally:
        db.close()
    run_now = not message_requests_queued_job(message) or message_requests_immediate_run(message)
    text = f"Created job: {title}. Focus switched to it."
    if run_now:
        deps.start_daemon(poll_seconds=0.0, quiet=True)
        text += " Started worker."
    if not quiet:
        print(text)
    return text


def queue_chat_note(
    job_id: str,
    message: str,
    *,
    deps: ChatControllerDeps,
    mode: str = "steer",
    quiet: bool = False,
) -> None:
    db, _config = deps.db_factory()
    try:
        entry = db.append_operator_message(job_id, message, source="chat", mode=mode)
        if not quiet:
            if entry.get("mode") == "follow_up":
                print(f"waiting after current branch: {entry['message']}")
            else:
                print(f"waiting: {entry['message']}")
    finally:
        db.close()
