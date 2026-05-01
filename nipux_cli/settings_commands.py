"""Slash-command handlers for inline Nipux configuration."""

from __future__ import annotations

import shlex
from contextlib import redirect_stdout
from io import StringIO

from nipux_cli.config import load_config
from nipux_cli.settings import config_field_value, inline_setting_notice
from nipux_cli.tui_commands import CHAT_SETTING_COMMANDS


def handle_chat_setting_command(command: str, rest: list[str]) -> bool:
    if command in {"key", "api-key"}:
        if not rest:
            config = load_config()
            state = "set" if config.model.api_key else "missing"
            print(f"API key is {state} via {config.model.api_key_env}. Use /api-key KEY to save a new one.")
            return True
        print(inline_setting_notice("secret:model.api_key", " ".join(rest)))
        return True
    if command not in CHAT_SETTING_COMMANDS:
        return False
    field, placeholder = CHAT_SETTING_COMMANDS[command]
    if not rest:
        current = config_field_value(field)
        print(f"{field} = {current}")
        print(f"usage: /{command} {placeholder}")
        return True
    print(inline_setting_notice(field, " ".join(rest)))
    return True


def capture_setting_command(line: str) -> list[str]:
    try:
        parts = shlex.split(line[1:] if line.startswith("/") else line)
    except ValueError as exc:
        return [f"parse error: {exc}"]
    if not parts:
        return []
    stream = StringIO()
    with redirect_stdout(stream):
        if not handle_chat_setting_command(parts[0], parts[1:]):
            print(f"unknown config command: /{parts[0]}")
    lines = [" ".join(item.split()) for item in stream.getvalue().splitlines() if item.strip()]
    return lines[-12:] or ["done"]
