"""First-run command decisions for the Nipux TUI."""

from __future__ import annotations

import shlex
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from typing import Callable

from nipux_cli.tui_commands import CHAT_SETTING_COMMANDS


@dataclass(frozen=True)
class FirstRunFrameDeps:
    capture_command: Callable[[str], list[str]]
    capture_setting_command: Callable[[str], list[str]]
    create_job: Callable[..., tuple[str, str]]
    current_default_job_id: Callable[[], str | None]
    extract_objective: Callable[[str], str]
    shell_command_names: set[str]


def handle_first_run_action(action: str, *, deps: FirstRunFrameDeps) -> tuple[str, str | list[str] | None]:
    if action.startswith("edit:"):
        return "edit", action.split(":", 1)[1]
    if action.startswith("secret:"):
        return "edit", action
    if action == "new":
        return "notice", "Type the goal in the input line, then press Enter."
    if action == "back":
        return "view", "start"
    if action == "jobs":
        return "notice", deps.capture_command("jobs")
    if action == "doctor":
        return "notice", deps.capture_command("doctor")
    if action == "init":
        return "notice", deps.capture_command("init")
    if action == "exit":
        return "exit", None
    return "notice", f"Unknown action: {action}"


def handle_first_run_frame_line(line: str, *, deps: FirstRunFrameDeps) -> tuple[str, str | list[str] | None]:
    original = line.strip()
    if original.startswith("/"):
        original = original[1:].strip()
    lowered = original.lower()
    if lowered in {"exit", "quit", ":q", "5"}:
        return "exit", None
    if lowered in {"clear"}:
        return "clear", None
    if lowered in {"help", "?", "commands"}:
        return "notice", [
            "Talk normally here, or ask Nipux to create a job with a concrete goal.",
            "Use the right pane for jobs, setup checks, and exit.",
            "When a job exists, the left pane becomes its chat and output stream.",
        ]
    if lowered in {"1", "new"}:
        return "notice", "Type `new OBJECTIVE` or paste the objective directly."
    if lowered.startswith("new "):
        return "open", create_first_run_job(original[4:].strip(), deps=deps)
    if lowered in {"2", "jobs", "ls"}:
        return "notice", deps.capture_command("jobs")
    if lowered == "settings":
        return "notice", "Config is changed with slash commands: /model, /api-key, /base-url, /context."
    if lowered in {"back"}:
        return "view", "start"
    if lowered in {"3", "doctor"}:
        return "notice", deps.capture_command("doctor")
    if lowered in {"4", "init"}:
        return "notice", deps.capture_command("init")
    if lowered == "shell":
        return "notice", "The old console is only available as `nipux shell` from your terminal."
    first = first_token(original)
    if first == "shell":
        return "notice", "The old console is only available as `nipux shell` from your terminal."
    if first in CHAT_SETTING_COMMANDS or first in {"api-key", "key"}:
        return "notice", deps.capture_setting_command(original)
    if first in deps.shell_command_names:
        before_job_id = deps.current_default_job_id()
        output = deps.capture_command(original)
        after_job_id = deps.current_default_job_id()
        if first == "create" and after_job_id and after_job_id != before_job_id:
            return "open", after_job_id
        return "notice", output
    objective = deps.extract_objective(original)
    if objective:
        return "open", create_first_run_job(objective, deps=deps)
    return "notice", first_run_chat_reply(original)


def first_run_chat_reply(message: str) -> str:
    lowered = message.strip().lower()
    if lowered in {"hi", "hello", "hey", "yo"}:
        return "Hi. Tell me what long-running work you want, or type /new followed by an objective."
    if "what can" in lowered or "help" in lowered:
        return "I can spin up long-running jobs, keep their output on the left, and let you monitor work from the right."
    return "I can chat here, but I only create a job when you give me a concrete goal like 'create a job to monitor nightly benchmarks'."


def create_first_run_job(objective: str, *, deps: FirstRunFrameDeps) -> str | list[str]:
    objective = objective.strip()
    if not objective:
        return ["No job created. Type an objective first."]
    job_id, _title = deps.create_job(objective=objective, title=None, kind="generic", cadence=None)
    return job_id


def capture_first_run_command(line: str, run_shell_line: Callable[[str], bool]) -> list[str]:
    stream = StringIO()
    with redirect_stdout(stream):
        try:
            run_shell_line(line)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print(f"command exited with status {exc.code}")
    lines = [" ".join(item.split()) for item in stream.getvalue().splitlines() if item.strip()]
    return lines[-8:] or ["done"]


def first_token(line: str) -> str:
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    return parts[0].lower() if parts else ""
