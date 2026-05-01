"""Slash command metadata and command-palette helpers for the TUI."""

from __future__ import annotations

from nipux_cli.tui_style import _accent, _bold, _fit_ansi, _muted


FIRST_RUN_SLASH_COMMANDS = [
    ("/new", "create a job"),
    ("/jobs", "list jobs"),
    ("/model", "set model"),
    ("/base-url", "set endpoint"),
    ("/api-key", "save key"),
    ("/api-key-env", "key env var"),
    ("/context", "token budget"),
    ("/input-cost", "input $/1M"),
    ("/timeout", "request timeout"),
    ("/home", "state directory"),
    ("/step-limit", "worker timeout"),
    ("/output-chars", "output preview size"),
    ("/output-cost", "output $/1M"),
    ("/daily-digest", "daily digest on/off"),
    ("/digest-time", "digest time"),
    ("/doctor", "check setup"),
    ("/init", "write config"),
    ("/help", "show commands"),
    ("/clear", "clear notices"),
    ("/exit", "quit"),
]

CHAT_SLASH_COMMANDS = [
    ("/run", "start worker"),
    ("/work", "one step"),
    ("/jobs", "switch jobs"),
    ("/focus", "set focus"),
    ("/history", "timeline"),
    ("/activity", "live feed"),
    ("/outcomes", "durable work"),
    ("/artifacts", "outputs"),
    ("/artifact", "open output"),
    ("/memory", "learning"),
    ("/usage", "tokens/cost"),
    ("/status", "job state"),
    ("/model", "set model"),
    ("/base-url", "set endpoint"),
    ("/api-key", "save key"),
    ("/api-key-env", "key env var"),
    ("/context", "token budget"),
    ("/input-cost", "input $/1M"),
    ("/timeout", "request timeout"),
    ("/home", "state directory"),
    ("/step-limit", "worker timeout"),
    ("/output-chars", "output preview size"),
    ("/output-cost", "output $/1M"),
    ("/daily-digest", "daily digest on/off"),
    ("/digest-time", "digest time"),
    ("/doctor", "check setup"),
    ("/pause", "pause job"),
    ("/resume", "resume job"),
    ("/stop", "pause job"),
    ("/new", "new job"),
    ("/exit", "quit"),
]

FIRST_RUN_ACTIONS = [
    ("new", "New job", "type a goal, then press enter"),
    ("jobs", "Jobs", "show saved workspaces"),
    ("doctor", "Doctor", "check local setup"),
    ("init", "Init", "write starter config"),
    ("exit", "Exit", "leave Nipux"),
]

SETTINGS_FIELD_TYPES = {
    "model.name": "str",
    "model.base_url": "str",
    "model.api_key_env": "str",
    "model.context_length": "int",
    "model.request_timeout_seconds": "float",
    "model.input_cost_per_million": "float",
    "model.output_cost_per_million": "float",
    "runtime.home": "path",
    "runtime.max_step_seconds": "int",
    "runtime.artifact_inline_char_limit": "int",
    "runtime.daily_digest_enabled": "bool",
    "runtime.daily_digest_time": "str",
}

CHAT_SETTING_COMMANDS = {
    "model": ("model.name", "MODEL"),
    "base-url": ("model.base_url", "URL"),
    "api-key-env": ("model.api_key_env", "ENV_NAME"),
    "context": ("model.context_length", "TOKENS"),
    "input-cost": ("model.input_cost_per_million", "DOLLARS_PER_1M_INPUT_TOKENS"),
    "output-cost": ("model.output_cost_per_million", "DOLLARS_PER_1M_OUTPUT_TOKENS"),
    "timeout": ("model.request_timeout_seconds", "SECONDS"),
    "home": ("runtime.home", "PATH"),
    "step-limit": ("runtime.max_step_seconds", "SECONDS"),
    "output-chars": ("runtime.artifact_inline_char_limit", "CHARS"),
    "daily-digest": ("runtime.daily_digest_enabled", "true|false"),
    "digest-time": ("runtime.daily_digest_time", "HH:MM"),
}

SLASH_ARGUMENT_HINTS = {
    "new": "OBJECTIVE",
    "focus": "JOB_TITLE",
    "switch": "JOB_TITLE",
    "delete": "JOB_TITLE",
    "history": "LIMIT",
    "events": "LIMIT",
    "outputs": "LIMIT",
    "artifact": "QUERY_OR_ID",
    "work": "N",
    "work-verbose": "N",
    "learn": "LESSON",
    "note": "MESSAGE",
    "follow": "MESSAGE",
    **{command: placeholder for command, (_field, placeholder) in CHAT_SETTING_COMMANDS.items()},
    "api-key": "KEY",
    "key": "KEY",
}


def slash_suggestion_lines(
    input_buffer: str,
    commands: list[tuple[str, str]],
    *,
    width: int,
    limit: int = 5,
) -> list[str]:
    if not input_buffer.startswith("/"):
        return []
    parts = input_buffer[1:].split(maxsplit=1)
    token = parts[0].lower() if parts else ""
    if " " in input_buffer[1:]:
        hint = SLASH_ARGUMENT_HINTS.get(token)
        description = next((desc for cmd, desc in commands if cmd == f"/{token}"), "")
        if not hint:
            return []
        body = f"{_accent('/' + token)} {_muted(hint)}"
        if description:
            body += f"  {_muted(description)}"
        return [
            _fit_ansi(_bold("Command"), width),
            _fit_ansi(body, width),
            _fit_ansi(_muted("enter sends"), width),
        ]
    all_matches = [(cmd, desc) for cmd, desc in commands if cmd[1:].startswith(token)]
    if not all_matches and token:
        all_matches = [(cmd, desc) for cmd, desc in commands if token in cmd[1:]]
    matches = all_matches[:limit]
    if not matches:
        return [
            _fit_ansi(_bold("Commands"), width),
            _fit_ansi(_muted("no matches"), width),
        ]
    cmd_width = min(14, max(len(cmd) for cmd, _ in matches) + 2)
    lines = [_fit_ansi(_bold("Commands") + _muted(f" {len(all_matches)}"), width)]
    for index, (cmd, desc) in enumerate(matches):
        marker = _accent("›") if index == 0 else _muted(" ")
        hint = SLASH_ARGUMENT_HINTS.get(cmd[1:])
        command_text = cmd if not hint else f"{cmd} {hint}"
        command_width = cmd_width + (len(hint) + 1 if hint else 0)
        body = f"{marker} {_fit_ansi(_accent(command_text), command_width)} {_muted(desc)}"
        lines.append(_fit_ansi(body, width))
    hidden = max(0, len(all_matches) - len(matches))
    if hidden:
        lines.append(_fit_ansi(_muted(f"+{hidden} more; type to filter"), width))
    else:
        lines.append(_fit_ansi(_muted("tab completes highlighted command"), width))
    return lines


def autocomplete_slash(input_buffer: str, commands: list[tuple[str, str]]) -> str:
    if not input_buffer.startswith("/") or " " in input_buffer.strip():
        return input_buffer
    token = input_buffer[1:].lower()
    matches = [cmd for cmd, _desc in commands if cmd[1:].startswith(token)]
    if not matches:
        matches = [cmd for cmd, _desc in commands if token in cmd[1:]]
    if not matches:
        return input_buffer
    return matches[0] + " "
