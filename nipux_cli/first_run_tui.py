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
from nipux_cli.tui_commands import FIRST_RUN_ACTIONS, FIRST_RUN_SLASH_COMMANDS, slash_suggestion_lines
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
    _status_badge,
    _style,
    _themed_lines,
)


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
    del daemon_text, view
    width = max(92, width)
    height = max(22, height)
    selected = _clamp_first_run_selection(selected)
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
        hint = "Enter sends  ·  ↑↓ setup  ·  Tab fills slash commands"
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
    left_lines = _install_left_lines(
        notices,
        config=config,
        width=left_width,
        rows=body_rows,
    )
    right_lines = _install_right_lines(
        jobs=jobs,
        config=config,
        home=home,
        config_path=config_path,
        selected=selected,
        width=right_width,
        rows=body_rows,
    )
    lines = [*header, _two_col_title(left_width, right_width, "Install", "Setup")]
    for index in range(body_rows):
        left = left_lines[index] if index < len(left_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        lines.append(_two_col_line(left, right, left_width=left_width, right_width=right_width))
    lines.extend(compose_lines)
    return "\n".join(first_run_themed_lines(lines[:height], width=width))


def first_run_columns(width: int) -> tuple[int, int]:
    right_width = max(42, int(width * 0.40))
    left_width = max(44, width - right_width - 3)
    if left_width + right_width + 3 > width:
        right_width = max(36, width - left_width - 3)
    return left_width, right_width


def first_run_themed_lines(lines: list[str], *, width: int) -> list[str]:
    return _themed_lines(lines, width=width)


def _install_left_lines(
    notices: list[str],
    *,
    config: AppConfig,
    width: int,
    rows: int,
) -> list[str]:
    if not notices:
        content = [
            *[_center_ansi(_style(line, "37;1"), width) for line in NIPUX_HERO],
            "",
            _center_ansi(_bold("Set up once. Then describe the work."), width),
            _center_ansi(_muted("Nipux keeps jobs running in the background and brings the history back here."), width),
            "",
            _center_ansi(_install_summary(config, width=max(24, width - 8)), width),
        ]
        top_pad = max(0, (rows - len(content)) // 2 - 1)
        return ([""] * top_pad + content)[:rows]

    lines = [
        _bold("Setup transcript"),
        _muted("Nipux shows setup answers and checks here. New jobs will open into the normal chat workspace."),
        "",
    ]
    for notice in notices[-8:]:
        normalized = " ".join(str(notice).split())
        wrapped = textwrap.wrap(normalized, width=max(24, width - 3))[:4] or [""]
        for index, part in enumerate(wrapped):
            prefix = _accent("› ") if index == 0 else "  "
            lines.append(_fit_ansi(prefix + part, width))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _install_right_lines(
    *,
    jobs: list[dict[str, Any]],
    config: AppConfig,
    home: str,
    config_path: str,
    selected: int,
    width: int,
    rows: int,
) -> list[str]:
    actions = _first_run_actions()
    lines = [
        _muted("Use Enter or click to edit the selected step."),
        "",
    ]
    for index, action in enumerate(actions):
        lines.append(_install_action_line(index, action, selected=selected, config=config, width=width))
    lines.extend(
        [
            "",
            _bold("Files"),
            f"{_muted('home')}   {_one_line(home, width - 7)}",
            f"{_muted('config')} {_one_line(config_path, width - 7)}",
            "",
            _bold("Jobs"),
        ]
    )
    if jobs:
        lines.extend(frame_jobs_lines(jobs, focused_job_id="", daemon_running=True, width=width)[:5])
    else:
        lines.append(_muted("No jobs yet. Finish setup, then create the first goal."))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _install_action_line(
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
    state = _action_state(key, config=config)
    return _fit_ansi(
        f"{marker} {index + 1}. {_fit_ansi(label_text, 12)} "
        f"{_fit_ansi(state, 9)} {_muted(_one_line(value, max(8, width - 29)))}",
        width,
    )


def _action_value(key: str, detail: str, *, config: AppConfig) -> str:
    if key.startswith("edit:"):
        field = key.split(":", 1)[1]
        return str(config_field_value(field, config))
    if key == "secret:model.api_key":
        return "stored in .env" if config.model.api_key else f"uses {config.model.api_key_env}"
    if key == "preset:local":
        return "local HTTP" if _is_local_endpoint(config.model.base_url) else "hosted HTTP; enter for local"
    return detail


def _action_state(key: str, *, config: AppConfig) -> str:
    if key == "preset:local":
        return _status_badge("ready") if _is_local_endpoint(config.model.base_url) else _status_badge("switch")
    if key == "secret:model.api_key":
        return _status_badge("ready") if config.model.api_key or _is_local_endpoint(config.model.base_url) else _status_badge("missing")
    if key.startswith("edit:"):
        return _status_badge("ready") if _action_value(key, "", config=config) else _status_badge("missing")
    if key == "doctor":
        return _status_badge("check")
    if key == "new":
        return _status_badge("next")
    return _status_badge("ready")


def _install_summary(config: AppConfig, *, width: int) -> str:
    connector = "local connector" if _is_local_endpoint(config.model.base_url) else "hosted connector"
    text = f"{connector} · {config.model.model} · {config.model.base_url}"
    return _muted(_one_line(text, width))


def _is_local_endpoint(value: str) -> bool:
    lowered = value.lower()
    return "localhost" in lowered or "127.0.0.1" in lowered or lowered.startswith("http://0.0.0.0")


def _first_run_actions() -> list[tuple[str, str, str]]:
    return FIRST_RUN_ACTIONS


def _clamp_first_run_selection(selected: int) -> int:
    actions = _first_run_actions()
    if not actions:
        return 0
    return max(0, min(selected, len(actions) - 1))
