"""First-run terminal UI rendering for Nipux."""

from __future__ import annotations

import textwrap
from typing import Any

from nipux_cli.config import AppConfig
from nipux_cli.settings import (
    config_field_value,
    edit_target_hint,
    edit_target_label,
    edit_target_masks_input,
)
from nipux_cli.tui_commands import FIRST_RUN_SLASH_COMMANDS, slash_suggestion_lines
from nipux_cli.tui_events import NIPUX_HERO
from nipux_cli.tui_layout import _compose_bar, _top_bar, _two_col_line, _two_col_title
from nipux_cli.tui_status import frame_jobs_lines
from nipux_cli.tui_style import (
    _accent,
    _bold,
    _center_ansi,
    _fit_ansi,
    _muted,
    _one_line,
    _style,
    _themed_lines,
)


INSTALL_FLOW = [
    ("model", "Model", "choose the model id"),
    ("connector", "Connector", "local or hosted endpoint"),
    ("endpoint", "Endpoint", "OpenAI-compatible /v1"),
    ("api", "API key", "secret stored in .env"),
    ("doctor", "Doctor", "check setup"),
    ("job", "First job", "describe long-running work"),
]


FIRST_RUN_ACTIONS_BY_VIEW: dict[str, list[tuple[str, str, str]]] = {
    "start": [
        ("view:model", "Begin setup", "walk through model, endpoint, key"),
        ("doctor", "Doctor", "check current environment"),
        ("exit", "Exit", "leave Nipux"),
    ],
    "model": [
        ("edit:model.name", "Edit model", "set provider/model id"),
        ("view:connector", "Continue", "choose connector"),
        ("view:start", "Back", "intro"),
    ],
    "connector": [
        ("preset:local", "Use local", "recommended first run"),
        ("view:endpoint", "Continue", "edit endpoint"),
        ("view:model", "Back", "model"),
    ],
    "endpoint": [
        ("edit:model.base_url", "Edit endpoint", "OpenAI-compatible /v1"),
        ("view:api", "Continue", "API key"),
        ("view:connector", "Back", "connector"),
    ],
    "api": [
        ("secret:model.api_key", "Save API key", "hidden input"),
        ("view:doctor", "Continue", "run doctor"),
        ("view:endpoint", "Back", "endpoint"),
    ],
    "doctor": [
        ("doctor", "Run doctor", "verify setup"),
        ("view:job", "Continue", "create work"),
        ("view:api", "Back", "API key"),
    ],
    "job": [
        ("new", "Create job", "type the goal below"),
        ("view:doctor", "Back", "doctor"),
        ("exit", "Exit", "leave Nipux"),
    ],
}


def build_first_run_frame(
    input_buffer: str,
    notices: list[str],
    *,
    width: int,
    height: int,
    selected: int = 0,
    view: str = "start",
    editing_field: str | None = None,
    config: AppConfig,
    daemon_text: str,
    jobs: list[dict[str, Any]],
    home: str,
    config_path: str,
) -> str:
    del daemon_text
    width = max(92, width)
    height = max(22, height)
    view = _normalize_first_run_view(view)
    selected = _clamp_first_run_selection(selected, view)
    header = _top_bar(
        width,
        state="setup",
        daemon="",
        model=config.model.model,
        base_url=config.model.base_url,
        context_length=config.model.context_length,
    )
    if editing_field:
        hint = edit_target_hint(editing_field, config)
        prompt_label = edit_target_label(editing_field)
    else:
        hint = _first_run_hint(view)
        prompt_label = "❯"
    suggestions = [] if editing_field else slash_suggestion_lines(input_buffer, FIRST_RUN_SLASH_COMMANDS, width=width)
    compose_lines = _compose_bar(
        input_buffer,
        width=width,
        hint=hint,
        suggestions=suggestions,
        prompt_label=prompt_label,
        mask_input=edit_target_masks_input(editing_field),
    )
    footer_rows = len(compose_lines)
    body_rows = max(10, height - len(header) - 1 - footer_rows)
    left_width, right_width = first_run_columns(width)
    left_lines = _wizard_left_lines(
        notices,
        config=config,
        view=view,
        width=left_width,
        rows=body_rows,
    )
    right_lines = _wizard_right_lines(
        jobs=jobs,
        config=config,
        home=home,
        config_path=config_path,
        selected=selected,
        view=view,
        width=right_width,
        rows=body_rows,
    )
    lines = [*header, _two_col_title(left_width, right_width, _left_title(view), "Setup")]
    for index in range(body_rows):
        left = left_lines[index] if index < len(left_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        lines.append(_two_col_line(left, right, left_width=left_width, right_width=right_width))
    lines.extend(compose_lines)
    return "\n".join(first_run_themed_lines(lines[:height], width=width))


def first_run_columns(width: int) -> tuple[int, int]:
    right_width = min(max(40, int(width * 0.34)), 54)
    left_width = max(48, width - right_width - 3)
    if left_width < 48:
        left_width = 48
        right_width = max(36, width - left_width - 3)
    return left_width, right_width


def first_run_actions(view: str) -> list[tuple[str, str, str]]:
    return FIRST_RUN_ACTIONS_BY_VIEW[_normalize_first_run_view(view)]


def first_run_themed_lines(lines: list[str], *, width: int) -> list[str]:
    return _themed_lines(lines, width=width)


def _wizard_left_lines(
    notices: list[str],
    *,
    config: AppConfig,
    view: str,
    width: int,
    rows: int,
) -> list[str]:
    if view == "start" and not notices:
        content = [
            *[_center_ansi(_style(line, "37;1"), width) for line in NIPUX_HERO],
            "",
            _center_ansi(_bold("Long-running work, installed in-session."), width),
            _center_ansi(_muted("Pick a model, connect an endpoint, run doctor, then create the first job."), width),
            "",
            _center_ansi(_install_summary(config, width=max(24, width - 8)), width),
            "",
            _center_ansi(_muted("Press Enter to begin. Type / for commands."), width),
        ]
        top_pad = max(0, (rows - len(content)) // 2 - 1)
        return ([""] * top_pad + content)[:rows]

    lines = [
        _bold(_screen_heading(view)),
        _muted(_screen_copy(view)),
        "",
        *_screen_value_lines(view, config=config, width=width),
    ]
    if notices:
        lines.extend(["", _bold("Transcript")])
        for notice in notices[-5:]:
            normalized = " ".join(str(notice).split())
            wrapped = textwrap.wrap(normalized, width=max(24, width - 4))[:3] or [""]
            for index, part in enumerate(wrapped):
                prefix = _accent("› ") if index == 0 else "  "
                lines.append(_fit_ansi(prefix + part, width))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _wizard_right_lines(
    *,
    jobs: list[dict[str, Any]],
    config: AppConfig,
    home: str,
    config_path: str,
    selected: int,
    view: str,
    width: int,
    rows: int,
) -> list[str]:
    actions = first_run_actions(view)
    lines = [
        _bold("Install"),
        *_stepper_lines(view, config=config, width=width),
        "",
        _bold("Actions"),
    ]
    for index, action in enumerate(actions):
        lines.append(_action_line(index, action, selected=selected, config=config, width=width))
    lines.extend([
        "",
        _bold("Profile"),
        f"{_muted('home')}   {_one_line(home, width - 7)}",
        f"{_muted('config')} {_one_line(config_path, width - 7)}",
        "",
        _bold("Jobs"),
    ])
    if jobs:
        lines.extend(frame_jobs_lines(jobs, focused_job_id="", daemon_running=True, width=width)[:4])
    else:
        lines.append(_muted("No jobs yet. The final screen creates one."))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _stepper_lines(view: str, *, config: AppConfig, width: int) -> list[str]:
    lines: list[str] = []
    for key, label, _detail in INSTALL_FLOW:
        marker = _accent("●") if key == view else _muted("○")
        state = _step_state(key, config=config)
        lines.append(_fit_ansi(f"{marker} {_fit_ansi(label, 10)} {_muted(state)}", width))
    return lines


def _action_line(
    index: int,
    action: tuple[str, str, str],
    *,
    selected: int,
    config: AppConfig,
    width: int,
) -> str:
    key, label, detail = action
    marker = _accent("›") if index == selected else _muted(" ")
    label_text = _bold(label) if index == selected else label
    value = _action_value(key, detail, config=config)
    return _fit_ansi(
        f"{marker} {index + 1}. {_fit_ansi(label_text, 15)} {_muted(_one_line(value, max(8, width - 21)))}",
        width,
    )


def _screen_value_lines(view: str, *, config: AppConfig, width: int) -> list[str]:
    if view == "model":
        return [_large_value("model", config.model.model, width=width)]
    if view == "connector":
        connector = "local" if _is_local_endpoint(config.model.base_url) else "hosted"
        return [
            _large_value("connector", connector, width=width),
            _muted("Enter on Use local to switch to http://localhost:8000/v1."),
        ]
    if view == "endpoint":
        return [_large_value("endpoint", config.model.base_url, width=width)]
    if view == "api":
        key_state = "set" if config.model.api_key else "missing"
        return [
            _large_value("key", key_state, width=width),
            _muted(f"Stored under {config.model.api_key_env} in ~/.nipux/.env."),
        ]
    if view == "doctor":
        return [
            _large_value("check", "ready to run", width=width),
            _muted("Doctor verifies the state directory, SQLite, model config, tools, and browser runtime."),
        ]
    if view == "job":
        return [
            _large_value("goal", "type it below", width=width),
            _muted("Paste an objective or type new OBJECTIVE. Nipux opens the main workspace after creation."),
        ]
    return []


def _large_value(label: str, value: str, *, width: int) -> str:
    label_text = _muted(f"{label} ")
    return _fit_ansi(label_text + _bold(_accent(_one_line(value, max(12, width - len(label) - 2)))), width)


def _action_value(key: str, detail: str, *, config: AppConfig) -> str:
    if key.startswith("view:"):
        return detail
    if key.startswith("edit:"):
        field = key.split(":", 1)[1]
        return str(config_field_value(field, config))
    if key == "secret:model.api_key":
        return "stored in .env" if config.model.api_key else f"uses {config.model.api_key_env}"
    if key == "preset:local":
        return "http://localhost:8000/v1"
    return detail


def _step_state(key: str, *, config: AppConfig) -> str:
    if key == "model":
        return _one_line(config.model.model, 20)
    if key == "connector":
        return "local" if _is_local_endpoint(config.model.base_url) else "hosted"
    if key == "endpoint":
        return _one_line(config.model.base_url, 20)
    if key == "api":
        return "ready" if config.model.api_key or _is_local_endpoint(config.model.base_url) else "missing"
    if key == "doctor":
        return "pending"
    if key == "job":
        return "next"
    return ""


def _first_run_hint(view: str) -> str:
    if view == "job":
        return "Type the first objective  ·  Enter creates work  ·  ↑↓ actions"
    return "Enter selects  ·  ↑↓ actions  ·  / commands"


def _left_title(view: str) -> str:
    return "Welcome" if view == "start" else _screen_heading(view)


def _screen_heading(view: str) -> str:
    return {
        "model": "Choose model",
        "connector": "Choose connector",
        "endpoint": "Connect endpoint",
        "api": "Add API key",
        "doctor": "Run checks",
        "job": "Create first job",
    }.get(view, "Welcome")


def _screen_copy(view: str) -> str:
    return {
        "model": "The chat controller and workers use this model unless you change it later.",
        "connector": "Local OpenAI-compatible servers are easiest to test; hosted providers need a key.",
        "endpoint": "Use any OpenAI-compatible /v1 endpoint. This stays generic and provider-neutral.",
        "api": "Hosted providers need a secret. Local endpoints can continue without one.",
        "doctor": "Run a setup check before opening the workspace.",
        "job": "The first job opens the main chat/workspace screen.",
    }.get(view, "Nipux installs through this full-screen setup.")


def _install_summary(config: AppConfig, *, width: int) -> str:
    connector = "local connector" if _is_local_endpoint(config.model.base_url) else "hosted connector"
    text = f"{connector} · {config.model.model} · {config.model.base_url}"
    return _muted(_one_line(text, width))


def _normalize_first_run_view(view: str) -> str:
    return view if view in FIRST_RUN_ACTIONS_BY_VIEW else "start"


def _is_local_endpoint(value: str) -> bool:
    lowered = value.lower()
    return "localhost" in lowered or "127.0.0.1" in lowered or lowered.startswith("http://0.0.0.0")


def _clamp_first_run_selection(selected: int, view: str) -> int:
    actions = first_run_actions(view)
    if not actions:
        return 0
    return max(0, min(selected, len(actions) - 1))
