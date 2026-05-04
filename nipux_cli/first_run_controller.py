"""First-run command decisions for the Nipux TUI."""

from __future__ import annotations

import shlex
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from typing import Callable

from nipux_cli.settings import config_field_value
from nipux_cli.tui_commands import CHAT_SETTING_COMMANDS
from nipux_cli.frame_snapshot import WORKSPACE_CHAT_ID


TOGGLE_SETTING_COMMANDS = {
    "tools.browser": "browser",
    "tools.web": "web",
    "tools.shell": "cli-access",
    "tools.files": "file-access",
}


@dataclass(frozen=True)
class FirstRunFrameDeps:
    capture_command: Callable[[str], list[str]]
    capture_setting_command: Callable[[str], list[str]]
    create_job: Callable[..., tuple[str, str]]
    current_default_job_id: Callable[[], str | None]
    extract_objective: Callable[[str], str]
    model_setup_verified: Callable[[], bool]
    verify_model_setup: Callable[[], list[str]]
    shell_command_names: set[str]


def handle_first_run_action(action: str, *, deps: FirstRunFrameDeps) -> tuple[str, str | list[str] | None]:
    if action == "open_workspace" and not deps.model_setup_verified():
        return "notice", "Run Doctor first. The workspace opens only after the configured model accepts a chat request."
    if action == "open_workspace":
        return "open", WORKSPACE_CHAT_ID
    if action.startswith("view:"):
        return "view", action.split(":", 1)[1]
    if action == "preset:local":
        notices = [
            *deps.capture_setting_command("model local-model"),
            *deps.capture_setting_command("base-url http://localhost:8000/v1"),
            *deps.capture_setting_command("api-key-env OPENAI_API_KEY"),
            "Local connector selected. Start your OpenAI-compatible server, then create a job.",
        ]
        return "notice", notices
    if action.startswith("toggle:"):
        field = action.split(":", 1)[1]
        command = TOGGLE_SETTING_COMMANDS.get(field)
        if not command:
            return "notice", f"Unknown setup toggle: {field}"
        next_value = "false" if bool(config_field_value(field)) else "true"
        return "notice", deps.capture_setting_command(f"{command} {next_value}")
    if action.startswith("edit:"):
        return "edit", action.split(":", 1)[1]
    if action.startswith("secret:"):
        return "edit", action
    if action == "new":
        return "notice", "Type the first job objective in the setup input, then press Enter."
    if action == "back":
        return "view", "endpoint"
    if action == "jobs":
        return "notice", deps.capture_command("jobs")
    if action == "doctor":
        return "notice", deps.verify_model_setup()
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
            "Finish setup before chat or jobs are available.",
            "Enter endpoint, API key, and model id when prompted.",
            "Doctor must verify the configured model before the workspace opens.",
        ]
    if lowered in {"1", "new"}:
        return "notice", "Finish setup first. Then tell Nipux what job to create from the chat workspace."
    if lowered.startswith("new "):
        result = create_first_run_job(original[4:].strip(), deps=deps)
        return ("open", result) if isinstance(result, str) else ("notice", result)
    if lowered in {"2", "jobs", "ls"}:
        return "notice", deps.capture_command("jobs")
    if lowered == "settings":
        return "notice", "Config is changed with slash commands: /model, /api-key, /base-url, /context."
    if lowered in {"back"}:
        return "notice", "Setup is linear during first run. Continue forward, then edit settings later if needed."
    if lowered in {"3", "doctor"}:
        return "notice", deps.verify_model_setup()
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
        result = create_first_run_job(objective, deps=deps)
        return ("open", result) if isinstance(result, str) else ("notice", result)
    return "notice", first_run_chat_reply(original)


def first_run_chat_reply(message: str) -> str:
    del message
    return "Setup must be completed before chat is available."


def create_first_run_job(objective: str, *, deps: FirstRunFrameDeps) -> str | list[str]:
    objective = objective.strip()
    if not objective:
        return ["No job created. Type an objective first."]
    if not deps.model_setup_verified():
        return [
            "No job created.",
            "Finish model setup first: choose a connector, set the endpoint/key if needed, then run Doctor.",
            "Doctor must confirm that the configured model accepts a chat request.",
        ]
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
