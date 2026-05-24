"""First-run terminal UI rendering for Nipux."""

from __future__ import annotations

from typing import Any

from nipux_cli.config import AppConfig
from nipux_cli.settings import (
    config_field_value,
    edit_target_hint,
    edit_target_label,
)
from nipux_cli.tui_style import (
    _accent,
    _bold,
    _center_ansi,
    _fit_ansi,
    _muted,
    _one_line,
    _strip_ansi,
    _themed_lines,
)


INSTALL_FLOW = [
    ("endpoint", "Endpoint", "OpenAI-compatible /v1"),
    ("api", "API key", "secret stored in .env"),
    ("model", "Model", "choose the model id"),
    ("access", "Tools", "browser, web, CLI, files"),
    ("doctor", "Doctor", "check setup"),
]


FIRST_RUN_ACTIONS_BY_VIEW: dict[str, list[tuple[str, str, str]]] = {
    "endpoint": [],
    "api": [],
    "model": [],
    "access": [
        ("toggle:tools.browser", "Browser", "automation"),
        ("toggle:tools.web", "Web", "search/extract"),
        ("toggle:tools.shell", "CLI", "terminal commands"),
        ("toggle:tools.files", "Files", "write files"),
        ("view:doctor", "Continue", "run checks"),
    ],
    "doctor": [
        ("doctor", "Run doctor", "verify setup"),
        ("open_workspace", "Open chat", "talk to Nipux"),
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
    editing_field = editing_field or _inline_edit_field_for_view(view)
    header: list[str] = []
    body_rows = max(10, height - len(header))
    body_lines = _wizard_body_lines(
        input_buffer=input_buffer,
        editing_field=editing_field,
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
    lines = [*header, *body_lines]
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
    input_buffer: str,
    editing_field: str | None,
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
    if view == "model":
        lines = _model_page_lines(
            config=config, input_buffer=input_buffer, editing_field=editing_field, selected=selected, width=width
        )
    elif view == "endpoint":
        lines = _endpoint_page_lines(
            config=config, input_buffer=input_buffer, editing_field=editing_field, selected=selected, width=width
        )
    elif view == "api":
        lines = _api_page_lines(
            config=config, input_buffer=input_buffer, editing_field=editing_field, selected=selected, width=width
        )
    elif view == "access":
        lines = _access_page_lines(config=config, selected=selected, width=width)
    elif view == "doctor":
        lines = _doctor_page_lines(config=config, selected=selected, width=width)
    else:
        lines = _endpoint_page_lines(
            config=config, input_buffer=input_buffer, editing_field=editing_field, selected=selected, width=width
        )
    if notices:
        lines = _append_notice_block(lines, notices, width=width, rows=rows)
    return _fit_page(lines, width=width, rows=rows)


def _model_page_lines(
    *,
    config: AppConfig,
    input_buffer: str,
    editing_field: str | None,
    selected: int,
    width: int,
) -> list[str]:
    del selected
    return [
        *_step_header("model", width=width),
        "",
        _center_ansi(_muted(_step_count_label("model")), width),
        _center_ansi(_bold("Enter the model id"), width),
        _center_ansi(_muted("This exact model powers chat replies and background workers."), width),
        "",
        *_field_panel(
            "MODEL ID",
            label="Model",
            field="model.name",
            current=config.model.model,
            input_buffer=input_buffer,
            editing_field=editing_field,
            width=min(84, width - 8),
            page_width=width,
        ),
        "",
        _center_ansi(_muted("Enter saves and advances. Blank input is not accepted."), width),
    ]


def _endpoint_page_lines(
    *,
    config: AppConfig,
    input_buffer: str,
    editing_field: str | None,
    selected: int,
    width: int,
) -> list[str]:
    del selected
    return [
        *_step_header("endpoint", width=width),
        "",
        _center_ansi(_muted(_step_count_label("endpoint")), width),
        _center_ansi(_bold("Enter the endpoint first"), width),
        _center_ansi(_muted("Use an OpenAI-compatible /v1 endpoint. Local or hosted both work."), width),
        "",
        *_field_panel(
            "BASE URL",
            label="Base URL",
            field="model.base_url",
            current=config.model.base_url,
            input_buffer=input_buffer,
            editing_field=editing_field,
            width=min(90, width - 8),
            page_width=width,
        ),
        "",
        _center_ansi(_muted("Example formats: http://localhost:8000/v1 or https://provider.example/v1"), width),
    ]


def _api_page_lines(
    *,
    config: AppConfig,
    input_buffer: str,
    editing_field: str | None,
    selected: int,
    width: int,
) -> list[str]:
    del selected
    key_state = "set" if config.model.api_key else "missing"
    return [
        *_step_header("api", width=width),
        "",
        _center_ansi(_muted(_step_count_label("api")), width),
        _center_ansi(_bold("Enter the API key"), width),
        _center_ansi(_muted("Hosted endpoints need a key. For a local endpoint, type skip."), width),
        "",
        *_field_panel(
            "API KEY",
            label="API key",
            field="secret:model.api_key",
            current=key_state,
            input_buffer=input_buffer,
            editing_field=editing_field,
            secret=True,
            width=min(84, width - 8),
            page_width=width,
        ),
        "",
        _center_ansi(_muted("Enter saves. Type skip only when the endpoint is local."), width),
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
        _center_ansi(_muted("Doctor calls the configured model before the workspace opens."), width),
        "",
        *_panel("DOCTOR", rows, width=min(90, width - 8), page_width=width),
        "",
        _center_ansi(
            _muted("If a check fails, edit with /base-url, /api-key, or /model, then run Doctor again."), width
        ),
        "",
        *_action_cards(first_run_actions("doctor"), selected=selected, config=config, width=width),
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
    cards = [
        _action_tile(index, action, selected=selected, config=config, width=card_width)
        for index, action in enumerate(actions)
    ]
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


def _field_panel(
    title: str,
    *,
    label: str,
    field: str,
    current: str,
    input_buffer: str,
    editing_field: str | None,
    width: int,
    page_width: int | None = None,
    secret: bool = False,
) -> list[str]:
    active = editing_field == field
    value = input_buffer if active else current
    rendered = _masked_inline_value(value, secret=secret, active=active)
    body = [f"{_muted(label + ': ')}{_bold(_accent(_one_line(rendered, max(12, width - len(label) - 8))))}"]
    return _panel(title, body, width=width, page_width=page_width)


def _masked_inline_value(value: str, *, secret: bool, active: bool) -> str:
    if secret:
        if not value:
            return "▌" if active else "missing"
        if value.lower() in {"skip", "none", "local"}:
            return value
        return "•" * min(max(len(value), 6), 24) + (" ▌" if active else "")
    if not value:
        return "▌" if active else "not set"
    return f"{value} ▌" if active else value


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
    visible = [notice for notice in notices if "cancelled edit" not in notice.lower()]
    if not visible:
        return lines
    notice = _center_ansi(_accent("› ") + _one_line(visible[-1], min(90, width - 4)), width)
    if len(lines) + 2 <= rows:
        return [*lines, "", notice]
    keep = max(0, rows - 2)
    return [*lines[:keep], "", notice]


def _fit_page(lines: list[str], *, width: int, rows: int) -> list[str]:
    fitted = [_fit_ansi(line, width) for line in lines]
    if len(fitted) >= 2 and "─" in _strip_ansi(fitted[1]):
        header = fitted[:2]
        body = _trim_blank_edges(fitted[2:])
        body_rows = max(0, rows - len(header))
        if len(body) < body_rows:
            extra = body_rows - len(body)
            top_pad = max(0, extra // 2)
            bottom_pad = extra - top_pad
            body = [" " * width for _ in range(top_pad)] + body + [" " * width for _ in range(bottom_pad)]
        return [*header, *body][:rows]
    if len(fitted) < rows:
        extra = rows - len(fitted)
        top_pad = max(0, extra // 2)
        bottom_pad = extra - top_pad
        fitted = [" " * width for _ in range(top_pad)] + fitted + [" " * width for _ in range(bottom_pad)]
    return fitted[:rows]


def _trim_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not _strip_ansi(lines[start]).strip():
        start += 1
    while end > start and not _strip_ansi(lines[end - 1]).strip():
        end -= 1
    return lines[start:end]


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
            _muted("Doctor verifies runtime checks, then sends a small chat request to the configured model."),
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
    if key == "endpoint":
        return _one_line(config.model.base_url, 20)
    if key == "api":
        return "ready" if config.model.api_key or _is_local_endpoint(config.model.base_url) else "missing"
    if key == "access":
        enabled = sum(
            bool(value) for value in (config.tools.browser, config.tools.web, config.tools.shell, config.tools.files)
        )
        return f"{enabled}/4 enabled"
    if key == "doctor":
        return "pending"
    return ""


def _first_run_hint(view: str) -> str:
    if view == "endpoint":
        return "Required: type an OpenAI-compatible endpoint URL, then Enter."
    if view == "api":
        return "Required: type an API key, or type skip for a local endpoint."
    if view == "model":
        return "Required: type the model id accepted by this endpoint."
    if view == "access":
        return "Use arrows/clicks to toggle tools, then choose Continue."
    if view == "doctor":
        return "Run Doctor, then open the chat workspace."
    return "Complete setup before the workspace opens."


def _first_run_edit_hint(field: str, config: AppConfig) -> str:
    if field == "model.base_url":
        return "Endpoint URL required. Enter saves and advances. Blank input is blocked."
    if field == "secret:model.api_key":
        return "API key required for hosted endpoints. For local endpoints, type skip."
    if field == "model.name":
        return "Model id required. Enter saves and advances. Blank input is blocked."
    return edit_target_hint(field, config)


def _first_run_prompt_label(field: str) -> str:
    if field == "model.base_url":
        return "Endpoint"
    if field == "secret:model.api_key":
        return "API key"
    if field == "model.name":
        return "Model"
    return edit_target_label(field)


def _left_title(view: str) -> str:
    return _screen_heading(view)


def _screen_heading(view: str) -> str:
    return {
        "model": "Choose model",
        "endpoint": "Connect endpoint",
        "api": "Add API key",
        "access": "Choose tools",
        "doctor": "Run checks",
    }.get(view, "Connect endpoint")


def _screen_copy(view: str) -> str:
    return {
        "model": "The chat controller and workers use this model unless you change it later.",
        "endpoint": "Use any OpenAI-compatible /v1 endpoint. This stays generic and provider-neutral.",
        "api": "Hosted providers need a secret. Local endpoints can continue without one.",
        "access": "Enable the generic tools this worker can use for any job.",
        "doctor": "Verify the configured model, then open the main chat workspace.",
    }.get(view, "Nipux installs through this full-screen setup.")


def _install_summary(config: AppConfig, *, width: int) -> str:
    connector = "local connector" if _is_local_endpoint(config.model.base_url) else "hosted connector"
    text = f"{connector} · {config.model.model} · {config.model.base_url}"
    return _muted(_one_line(text, width))


def _normalize_first_run_view(view: str) -> str:
    return view if view in FIRST_RUN_ACTIONS_BY_VIEW else "endpoint"


def _inline_edit_field_for_view(view: str) -> str | None:
    return {
        "endpoint": "model.base_url",
        "api": "secret:model.api_key",
        "model": "model.name",
    }.get(view)


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
