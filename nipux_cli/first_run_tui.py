"""First-run terminal UI rendering for Nipux."""

from __future__ import annotations

import textwrap
from typing import Any

from nipux_cli.config import AppConfig, load_config
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
    width = max(92, width)
    height = max(22, height)
    header = _top_bar(width, state="setup", daemon=daemon_text, model=config.model.model)
    if editing_field:
        hint = edit_target_hint(editing_field, config)
        prompt_label = edit_target_label(editing_field)
    else:
        hint = "Type a goal to create a job. / opens commands. ↑↓ moves. Click selects."
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
    left_lines = _first_run_left_lines(
        notices,
        width=left_width,
        rows=body_rows,
        view=view,
        selected=selected,
    )
    right_lines = _first_run_right_lines(
        jobs=jobs,
        daemon_text=daemon_text,
        model=config.model.model,
        home=home,
        config_path=config_path,
        selected=selected,
        view=view,
        width=right_width,
        rows=body_rows,
    )
    lines = [*header, _two_col_title(left_width, right_width, "Nipux Chat", "Control")]
    for index in range(body_rows):
        left = left_lines[index] if index < len(left_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        lines.append(_two_col_line(left, right, left_width=left_width, right_width=right_width))
    lines.extend(compose_lines)
    return "\n".join(first_run_themed_lines(lines[:height], width=width))


def first_run_columns(width: int) -> tuple[int, int]:
    left_width = max(56, int(width * 0.60))
    right_width = max(34, width - left_width - 3)
    if right_width < 34:
        right_width = 34
        left_width = max(56, width - right_width - 3)
    return left_width, right_width


def first_run_themed_lines(lines: list[str], *, width: int) -> list[str]:
    return _themed_lines(lines, width=width)


def _first_run_left_lines(
    notices: list[str],
    *,
    width: int,
    rows: int,
    view: str,
    selected: int,
) -> list[str]:
    selected_label = _first_run_actions(view)[_clamp_first_run_selection(selected, view)][1]
    if not notices:
        content = [
            *[_center_ansi(_style(line, "37;1"), width) for line in NIPUX_HERO],
            "",
            _center_ansi(_muted("A long-running agent harness."), width),
            _center_ansi(_muted("Type an objective, or use slash commands for setup."), width),
            "",
            _center_ansi(f"{_muted('Selected')} {_accent(selected_label)}", width),
        ]
        top_pad = max(0, (rows - len(content)) // 2 - 1)
        return ([""] * top_pad + content)[:rows]
    lines = [
        _bold("No agent output yet."),
        _muted("Create a job from the control pane or type a goal in the input line."),
        "",
        f"{_muted('Selected')} {_accent(selected_label)}",
        "",
        f"{_muted('Mode')} {_accent('Start')}",
        "",
        _muted("After a job exists, this side becomes the agent conversation and output stream."),
        "",
        _muted("Use arrows, Enter, click, or / commands. Controls stay on the right."),
        "",
        _muted("Recent"),
    ]
    for notice in notices[-6:]:
        for wrapped in textwrap.wrap(" ".join(str(notice).split()), width=max(20, width - 4))[:3]:
            lines.append(f"{_accent('›')} {wrapped}")
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _first_run_right_lines(
    *,
    jobs: list[dict[str, Any]],
    daemon_text: str,
    model: str,
    home: str,
    config_path: str,
    selected: int,
    view: str,
    width: int,
    rows: int,
) -> list[str]:
    profile_lines = _first_run_profile_lines(
        model=model,
        daemon_text=daemon_text,
        home=home,
        config_path=config_path,
        width=width,
    )
    lines = [
        *profile_lines,
        _bold("Actions"),
        *_first_run_action_lines(_first_run_actions(view), selected, width=width),
        "",
        _bold("Jobs"),
    ]
    if jobs:
        lines.extend(frame_jobs_lines(jobs, focused_job_id="", daemon_running=False, width=width)[:5])
    else:
        lines.append(_muted("No saved jobs in this profile."))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _first_run_profile_lines(
    *,
    model: str,
    daemon_text: str,
    home: str,
    config_path: str,
    width: int,
) -> list[str]:
    return [
        f"{_muted('Mode')}   {_accent('Start')}",
        f"{_muted('Model')}  {_one_line(model, width - 8)}",
        f"{_muted('Daemon')} {_one_line(daemon_text, width - 8)}",
        f"{_muted('Home')}   {_one_line(home, width - 8)}",
        f"{_muted('Config')} {_one_line(config_path, width - 8)}",
        "",
    ]


def _first_run_action_lines(actions: list[tuple[str, str, str]], selected: int, *, width: int) -> list[str]:
    lines: list[str] = []
    selected = max(0, min(selected, len(actions) - 1)) if actions else 0
    for index, (key, label, detail) in enumerate(actions):
        marker = _accent("›") if index == selected else _muted(" ")
        name = _bold(label) if index == selected else label
        if key.startswith("edit:"):
            field = key.split(":", 1)[1]
            detail = _one_line(str(config_field_value(field)), max(10, width - 18))
        elif key == "secret:model.api_key":
            config = load_config()
            detail = "set" if config.model.api_key else "missing"
        lines.append(_fit_ansi(f"{marker} {_fit_ansi(name, 14)} {_muted(detail)}", width))
    return lines


def _first_run_actions(view: str) -> list[tuple[str, str, str]]:
    del view
    return FIRST_RUN_ACTIONS


def _clamp_first_run_selection(selected: int, view: str) -> int:
    actions = _first_run_actions(view)
    if not actions:
        return 0
    return max(0, min(selected, len(actions) - 1))
