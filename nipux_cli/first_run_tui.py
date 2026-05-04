"""First-run terminal UI rendering for Nipux."""

from __future__ import annotations

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
from nipux_cli.tui_layout import _compose_bar
from nipux_cli.tui_style import (
    _accent,
    _bold,
    _center_ansi,
    _fit_ansi,
    _muted,
    _one_line,
    _style,
    _strip_ansi,
    _themed_lines,
)


INSTALL_FLOW = [
    ("model", "Model", "choose the model id"),
    ("connector", "Connector", "local or hosted endpoint"),
    ("endpoint", "Endpoint", "OpenAI-compatible /v1"),
    ("api", "API key", "secret stored in .env"),
    ("access", "Tools", "browser, web, CLI, files"),
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
        ("view:access", "Continue", "choose tool access"),
        ("view:endpoint", "Back", "endpoint"),
    ],
    "access": [
        ("toggle:tools.browser", "Browser", "automation"),
        ("toggle:tools.web", "Web", "search/extract"),
        ("toggle:tools.shell", "CLI", "terminal commands"),
        ("toggle:tools.files", "Files", "write files"),
        ("view:doctor", "Continue", "run checks"),
    ],
    "doctor": [
        ("doctor", "Run doctor", "verify setup"),
        ("view:job", "Continue", "create work"),
        ("view:access", "Back", "tool access"),
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
    header: list[str] = []
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
    body_lines = _wizard_body_lines(
        notices=notices,
        jobs=jobs,
        config=config,
        home=home,
        config_path=config_path,
        selected=selected,
        view=view,
        width=width,
        rows=body_rows,
    )
    lines = [*header, *body_lines, *compose_lines]
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


def _wizard_body_lines(
    *,
    notices: list[str],
    jobs: list[dict[str, Any]],
    config: AppConfig,
    home: str,
    config_path: str,
    selected: int,
    view: str,
    width: int,
    rows: int,
) -> list[str]:
    if view == "start":
        lines = _start_page_lines(config=config, selected=selected, width=width, rows=rows)
    elif view == "model":
        lines = _model_page_lines(config=config, selected=selected, width=width)
    elif view == "connector":
        lines = _connector_page_lines(config=config, selected=selected, width=width)
    elif view == "endpoint":
        lines = _endpoint_page_lines(config=config, selected=selected, width=width)
    elif view == "api":
        lines = _api_page_lines(config=config, selected=selected, width=width)
    elif view == "access":
        lines = _access_page_lines(config=config, selected=selected, width=width)
    elif view == "doctor":
        lines = _doctor_page_lines(config=config, selected=selected, width=width)
    elif view == "job":
        lines = _job_page_lines(
            jobs=jobs,
            config=config,
            home=home,
            config_path=config_path,
            selected=selected,
            width=width,
        )
    else:
        lines = _start_page_lines(config=config, selected=selected, width=width, rows=rows)
    if notices:
        lines = _append_notice_block(lines, notices, width=width, rows=rows)
    return _fit_page(lines, width=width, rows=rows)


def _start_page_lines(*, config: AppConfig, selected: int, width: int, rows: int) -> list[str]:
    actions = first_run_actions("start")
    content = [
        *[_center_ansi(_style(line, "37;1"), width) for line in NIPUX_HERO],
        "",
        _center_ansi(_bold("Long-running work, installed in-session."), width),
        _center_ansi(_muted("A local-first setup flow opens before any workspace exists."), width),
        "",
        _center_ansi(_install_summary(config, width=max(24, width - 12)), width),
        "",
        *_action_cards(actions, selected=selected, config=config, width=width),
        "",
        _center_ansi(_muted("Enter selects  ·  ←→ moves  ·  / opens commands"), width),
    ]
    top_pad = max(0, (rows - len(content)) // 2 - 1)
    return [""] * top_pad + content


def _model_page_lines(*, config: AppConfig, selected: int, width: int) -> list[str]:
    return [
        *_step_header("model", width=width),
        "",
        _center_ansi(_muted(_step_count_label("model")), width),
        _center_ansi(_bold("Choose the model"), width),
        _center_ansi(_muted("This model powers chat replies and background workers until you change it."), width),
        "",
        *_panel(
            "MODEL ID",
            [_bold(_accent(config.model.model)), _muted("Use any provider/model name your endpoint accepts.")],
            width=min(84, width - 8),
            page_width=width,
        ),
        "",
        *_action_cards(first_run_actions("model"), selected=selected, config=config, width=width),
    ]


def _connector_page_lines(*, config: AppConfig, selected: int, width: int) -> list[str]:
    connector = "local" if _is_local_endpoint(config.model.base_url) else "hosted"
    local_card = _choice_card(
        "LOCAL",
        "Use an OpenAI-compatible server on this machine.",
        "http://localhost:8000/v1",
        active=connector == "local",
        width=max(30, (width - 10) // 2),
    )
    hosted_card = _choice_card(
        "HOSTED",
        "Use OpenRouter, OpenAI, or another compatible provider.",
        "requires endpoint + key",
        active=connector != "local",
        width=max(30, (width - 10) // 2),
    )
    return [
        *_step_header("connector", width=width),
        "",
        _center_ansi(_muted(_step_count_label("connector")), width),
        _center_ansi(_bold("Choose a connector"), width),
        _center_ansi(_muted("Pick local for first install, or keep a hosted endpoint if you already configured one."), width),
        "",
        *_join_cards(local_card, hosted_card, width=width),
        "",
        *_action_cards(first_run_actions("connector"), selected=selected, config=config, width=width),
    ]


def _endpoint_page_lines(*, config: AppConfig, selected: int, width: int) -> list[str]:
    return [
        *_step_header("endpoint", width=width),
        "",
        _center_ansi(_muted(_step_count_label("endpoint")), width),
        _center_ansi(_bold("Connect an endpoint"), width),
        _center_ansi(_muted("Nipux talks to OpenAI-compatible /v1 APIs. Nothing here is tied to one provider."), width),
        "",
        *_form_panel(
            "BASE URL",
            config.model.base_url,
            "/base-url URL",
            width=min(90, width - 8),
            page_width=width,
        ),
        "",
        *_action_cards(first_run_actions("endpoint"), selected=selected, config=config, width=width),
    ]


def _api_page_lines(*, config: AppConfig, selected: int, width: int) -> list[str]:
    key_state = "set" if config.model.api_key else "missing"
    key_color = _style(key_state, "32" if key_state == "set" else "33")
    return [
        *_step_header("api", width=width),
        "",
        _center_ansi(_muted(_step_count_label("api")), width),
        _center_ansi(_bold("Add a secret"), width),
        _center_ansi(_muted("Local endpoints can continue without a key. Hosted providers usually need one."), width),
        "",
        *_panel(
            "API KEY",
            [
                f"{_muted('state')} {key_color}",
                f"{_muted('env')}   {_bold(config.model.api_key_env)}",
                _muted("Stored in the local Nipux env file, never in repository config."),
            ],
            width=min(84, width - 8),
            page_width=width,
        ),
        "",
        *_action_cards(first_run_actions("api"), selected=selected, config=config, width=width),
    ]


def _access_page_lines(*, config: AppConfig, selected: int, width: int) -> list[str]:
    rows = [
        _access_row("browser", config.tools.browser, "persistent browser automation"),
        _access_row("web", config.tools.web, "web search and page extraction"),
        _access_row("CLI", config.tools.shell, "bounded terminal commands"),
        _access_row("files", config.tools.files, "write deliverables into the workspace"),
    ]
    return [
        *_step_header("access", width=width),
        "",
        _center_ansi(_muted(_step_count_label("access")), width),
        _center_ansi(_bold("Choose tool access"), width),
        _center_ansi(_muted("These switches control the generic tools workers can call for any job."), width),
        "",
        *_panel("TOOL ACCESS", rows, width=min(90, width - 8), page_width=width),
        "",
        *_action_cards(first_run_actions("access"), selected=selected, config=config, width=width),
    ]


def _doctor_page_lines(*, config: AppConfig, selected: int, width: int) -> list[str]:
    checks = [
        ("state directory", "writable under ~/.nipux or NIPUX_HOME"),
        ("database", "SQLite state store can open"),
        ("model config", f"{config.model.model} at {config.model.base_url}"),
        (
            "tools",
            f"browser={config.tools.browser} web={config.tools.web} CLI={config.tools.shell} files={config.tools.files}",
        ),
    ]
    rows = [f"{_accent('✓')} {_fit_ansi(name, 18)} {_muted(detail)}" for name, detail in checks]
    return [
        *_step_header("doctor", width=width),
        "",
        _center_ansi(_muted(_step_count_label("doctor")), width),
        _center_ansi(_bold("Run checks"), width),
        _center_ansi(_muted("Doctor verifies the local runtime before the first job opens."), width),
        "",
        *_panel("DOCTOR", rows, width=min(90, width - 8), page_width=width),
        "",
        *_action_cards(first_run_actions("doctor"), selected=selected, config=config, width=width),
    ]


def _job_page_lines(
    *,
    jobs: list[dict[str, Any]],
    config: AppConfig,
    home: str,
    config_path: str,
    selected: int,
    width: int,
) -> list[str]:
    job_state = f"{len(jobs)} saved job" + ("" if len(jobs) == 1 else "s")
    return [
        *_step_header("job", width=width),
        "",
        _center_ansi(_muted(_step_count_label("job")), width),
        _center_ansi(_bold("Create the first job"), width),
        _center_ansi(_muted("Type a real objective in the composer. Nipux opens chat/workspace after creation."), width),
        "",
        *_panel(
            "READY",
            [
                f"{_muted('model')}  {_bold(config.model.model)}",
                f"{_muted('home')}   {_one_line(home, 64)}",
                f"{_muted('config')} {_one_line(config_path, 64)}",
                f"{_muted('jobs')}   {job_state}",
            ],
            width=min(86, width - 8),
            page_width=width,
        ),
        "",
        *_action_cards(first_run_actions("job"), selected=selected, config=config, width=width),
    ]


def _stepper_lines(view: str, *, config: AppConfig, width: int) -> list[str]:
    lines: list[str] = []
    for key, label, _detail in INSTALL_FLOW:
        marker = _accent("●") if key == view else _muted("○")
        state = _step_state(key, config=config)
        lines.append(_fit_ansi(f"{marker} {_fit_ansi(label, 10)} {_muted(state)}", width))
    return lines


def _step_header(view: str, *, width: int) -> list[str]:
    parts = []
    for index, (key, label, _detail) in enumerate(INSTALL_FLOW, start=1):
        marker = _accent("●") if key == view else _muted("○")
        text = _bold(label) if key == view else _muted(label)
        parts.append(f"{marker} {index} {text}")
    return [
        _center_ansi("   ".join(parts), width),
        _muted("─" * width),
    ]


def _action_cards(
    actions: list[tuple[str, str, str]],
    *,
    selected: int,
    config: AppConfig,
    width: int,
) -> list[str]:
    if not actions:
        return []
    gap = 2
    card_width = max(18, min(34, (width - (len(actions) - 1) * gap - 4) // len(actions)))
    cards = [_action_tile(index, action, selected=selected, config=config, width=card_width) for index, action in enumerate(actions)]
    rows = _join_many_cards(cards, gap=gap, width=width)
    return [_center_ansi(row.rstrip(), width) for row in rows]


def _action_tile(
    index: int,
    action: tuple[str, str, str],
    *,
    selected: int,
    config: AppConfig,
    width: int,
) -> list[str]:
    key, label, detail = action
    active = index == selected
    border = _accent if active else _muted
    marker = _accent("›") if active else _muted(" ")
    label_text = _bold(label) if active else label
    value = _action_value(key, detail, config=config)
    inner = max(8, width - 4)
    return [
        border("╭" + "─" * (width - 2) + "╮"),
        border("│ ") + _fit_ansi(f"{marker} {index + 1}. {label_text}", inner) + border(" │"),
        border("│ ") + _fit_ansi(_muted(_one_line(value, inner)), inner) + border(" │"),
        border("╰" + "─" * (width - 2) + "╯"),
    ]


def _panel(title: str, body: list[str], *, width: int, page_width: int | None = None) -> list[str]:
    width = max(32, width)
    inner = max(8, width - 4)
    title_text = f" {title} "
    lines = [_muted("╭─" + title_text + "─" * max(0, width - len(title_text) - 3) + "╮")]
    for item in body:
        lines.append(_muted("│ ") + _fit_ansi(item, inner) + _muted(" │"))
    lines.append(_muted("╰" + "─" * (width - 2) + "╯"))
    return [_center_ansi(line, page_width or width) for line in lines]


def _form_panel(title: str, value: str, command: str, *, width: int, page_width: int | None = None) -> list[str]:
    return _panel(
        title,
        [
            _bold(_accent(_one_line(value, max(16, width - 10)))),
            _muted(f"edit with {command} or press Enter on Edit endpoint"),
        ],
        width=width,
        page_width=page_width,
    )


def _choice_card(title: str, copy: str, value: str, *, active: bool, width: int) -> list[str]:
    border = _accent if active else _muted
    marker = _accent("● selected") if active else _muted("○ available")
    inner = max(8, width - 4)
    return [
        border("╭" + "─" * (width - 2) + "╮"),
        border("│ ") + _fit_ansi(_bold(title), inner) + border(" │"),
        border("│ ") + _fit_ansi(marker, inner) + border(" │"),
        border("│ ") + _fit_ansi(_muted(copy), inner) + border(" │"),
        border("│ ") + _fit_ansi(_accent(value), inner) + border(" │"),
        border("╰" + "─" * (width - 2) + "╯"),
    ]


def _join_cards(left: list[str], right: list[str], *, width: int) -> list[str]:
    gap = "  "
    rows = []
    for index in range(max(len(left), len(right))):
        left_line = left[index] if index < len(left) else " " * len(_strip_ansi(left[0]))
        right_line = right[index] if index < len(right) else " " * len(_strip_ansi(right[0]))
        rows.append(_center_ansi(left_line + gap + right_line, width))
    return rows


def _join_many_cards(cards: list[list[str]], *, gap: int, width: int) -> list[str]:
    rows: list[str] = []
    max_rows = max(len(card) for card in cards)
    gap_text = " " * gap
    for row_index in range(max_rows):
        row_parts = []
        for card in cards:
            fallback_width = len(_strip_ansi(card[0]))
            row_parts.append(card[row_index] if row_index < len(card) else " " * fallback_width)
        rows.append(gap_text.join(row_parts))
    return [_fit_ansi(row, width) for row in rows]


def _append_notice_block(lines: list[str], notices: list[str], *, width: int, rows: int) -> list[str]:
    budget = max(3, min(6, rows // 4))
    notice_lines = [_bold("Transcript")]
    for notice in notices[-budget:]:
        notice_lines.append(_fit_ansi(_accent("› ") + _one_line(notice, width - 4), width))
    if len(lines) + len(notice_lines) + 1 <= rows:
        return [*lines, "", *notice_lines]
    keep = max(0, rows - len(notice_lines) - 1)
    return [*lines[:keep], "", *notice_lines]


def _fit_page(lines: list[str], *, width: int, rows: int) -> list[str]:
    fitted = [_fit_ansi(line, width) for line in lines]
    if len(fitted) < rows:
        fitted.extend([" " * width for _ in range(rows - len(fitted))])
    return fitted[:rows]


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
    if key.startswith("toggle:"):
        field = key.split(":", 1)[1]
        return "enabled" if bool(config_field_value(field, config)) else "disabled"
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
    if key == "access":
        enabled = sum(bool(value) for value in (config.tools.browser, config.tools.web, config.tools.shell, config.tools.files))
        return f"{enabled}/4 enabled"
    if key == "doctor":
        return "pending"
    if key == "job":
        return "next"
    return ""


def _first_run_hint(view: str) -> str:
    if view == "job":
        return "Type the first objective  ·  Enter creates work  ·  ←→ actions"
    return "Enter selects  ·  ←→ actions  ·  / commands"


def _left_title(view: str) -> str:
    return "Welcome" if view == "start" else _screen_heading(view)


def _screen_heading(view: str) -> str:
    return {
        "model": "Choose model",
        "connector": "Choose connector",
        "endpoint": "Connect endpoint",
        "api": "Add API key",
        "access": "Choose tools",
        "doctor": "Run checks",
        "job": "Create first job",
    }.get(view, "Welcome")


def _screen_copy(view: str) -> str:
    return {
        "model": "The chat controller and workers use this model unless you change it later.",
        "connector": "Local OpenAI-compatible servers are easiest to test; hosted providers need a key.",
        "endpoint": "Use any OpenAI-compatible /v1 endpoint. This stays generic and provider-neutral.",
        "api": "Hosted providers need a secret. Local endpoints can continue without one.",
        "access": "Enable the generic tools this worker can use for any job.",
        "doctor": "Run a setup check before opening the workspace.",
        "job": "The first job opens the main chat/workspace screen.",
    }.get(view, "Nipux installs through this full-screen setup.")


def _install_summary(config: AppConfig, *, width: int) -> str:
    connector = "local connector" if _is_local_endpoint(config.model.base_url) else "hosted connector"
    text = f"{connector} · {config.model.model} · {config.model.base_url}"
    return _muted(_one_line(text, width))


def _normalize_first_run_view(view: str) -> str:
    return view if view in FIRST_RUN_ACTIONS_BY_VIEW else "start"


def _step_count_label(view: str) -> str:
    keys = [key for key, _label, _detail in INSTALL_FLOW]
    try:
        index = keys.index(view) + 1
    except ValueError:
        index = 1
    return f"STEP {index} / {len(INSTALL_FLOW)}"


def _access_row(name: str, enabled: bool, detail: str) -> str:
    marker = _accent("on ") if enabled else _muted("off")
    return f"{_fit_ansi(name, 10)} {marker} {_muted(detail)}"


def _is_local_endpoint(value: str) -> bool:
    lowered = value.lower()
    return "localhost" in lowered or "127.0.0.1" in lowered or lowered.startswith("http://0.0.0.0")


def _clamp_first_run_selection(selected: int, view: str) -> int:
    actions = first_run_actions(view)
    if not actions:
        return 0
    return max(0, min(selected, len(actions) - 1))
