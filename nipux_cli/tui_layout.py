"""Reusable terminal layout primitives for Nipux frames."""

from __future__ import annotations

from typing import Any

from nipux_cli.tui_style import (
    _accent,
    _bold,
    _fancy_ui,
    _fit_ansi,
    _muted,
    _one_line,
    _strip_ansi,
    _style,
)


def _top_bar(
    width: int,
    *,
    state: str,
    daemon: str,
    model: str,
    token_usage: dict[str, Any] | None = None,
    context_length: int = 0,
    base_url: str = "",
) -> list[str]:
    dots = f"{_style('●', '31')} {_style('●', '33')} {_style('●', '32')}  " if _fancy_ui() else ""
    title = f"{dots}{_bold(_accent('Nipux CLI'))} {_status_dot(state)}"
    daemon_compact = "running" if daemon.startswith("running") else "stopped"
    runtime = _pill("daemon", daemon_compact)
    usage_text = _token_usage_topline(token_usage or {}, context_length=context_length, model=model, base_url=base_url)
    model_line = f"{_muted('model')} {_style(_one_line(model, max(16, width // 3)), '36')}"
    first = _edge_line(title, runtime, width=width)
    second = _edge_line(model_line, usage_text, width=width)
    return [
        first,
        second,
        _muted("─" * width),
    ]


def _two_col_title(left_width: int, right_width: int, left: str, right: str) -> str:
    return _muted("╭─ ") + _fit_ansi(_bold(left), max(0, left_width - 3)) + _muted(" │ ") + _fit_ansi(_bold(right), right_width)


def _two_col_line(left: str, right: str, *, left_width: int, right_width: int) -> str:
    return _fit_ansi(left, left_width) + _muted(" │ ") + _fit_ansi(right, right_width)


def _edge_line(left: str, right: str, *, width: int) -> str:
    right_len = len(_strip_ansi(right))
    left_width = max(0, width - right_len - 2)
    left_text = _fit_ansi(left, left_width)
    gap = max(1, width - len(_strip_ansi(left_text)) - right_len)
    return _fit_ansi(left_text + " " * gap + right, width)


def _compose_bar(
    input_buffer: str,
    *,
    width: int,
    hint: str | None = None,
    suggestions: list[str] | None = None,
    prompt_label: str = "❯",
    mask_input: bool = False,
) -> list[str]:
    if mask_input:
        visible_input = "•" * min(len(input_buffer), max(8, width - 8))
    else:
        visible_input = input_buffer[-max(8, width - 8) :]
    hint = _muted(hint or "Enter sends  /jobs switches  /run starts  /help commands")
    label = _accent(prompt_label) if prompt_label == "❯" else _muted(prompt_label)
    prompt = f"{label} {visible_input}{_accent('▌')}"
    title = " Compose "
    lines = []
    if suggestions:
        lines.extend(suggestions)
    lines.extend(
        [
            _muted("╭─") + _bold(title) + _muted("─" * max(0, width - len(title) - 2)),
            _fit_ansi(hint, width),
            _fit_ansi(prompt, width),
        ]
    )
    return lines


def _metric_strip(items: list[tuple[str, Any]], *, width: int) -> str:
    parts = [f"{_muted(label)} {_bold(value)}" for label, value in items]
    text = "  ".join(parts)
    if len(_strip_ansi(text)) <= width:
        return text
    compact = [f"{label}:{value}" for label, value in items]
    return _one_line("  ".join(compact), width)


def _pill(label: str, value: Any) -> str:
    value_text = str(value)
    color = "36"
    lowered = value_text.lower()
    if any(term in lowered for term in ("running", "active", "advancing", "ok")):
        color = "32"
    elif any(term in lowered for term in ("paused", "idle", "queued", "planning")):
        color = "33"
    elif any(term in lowered for term in ("failed", "cancelled", "error", "stopped")):
        color = "31"
    return f"{_muted(label)} {_style(value_text, color)}"


def _token_usage_topline(
    usage: dict[str, Any],
    *,
    context_length: int,
    model: str,
    base_url: str,
) -> str:
    calls = _safe_int(usage.get("calls"))
    if calls <= 0:
        return f"{_muted('ctx')} {_style('0', '36')}  {_muted('out')} {_style('0', '36')}  {_muted('cost')} {_style('$0.00', '36')}"
    latest_prompt = _safe_int(usage.get("latest_prompt_tokens"))
    completion = _safe_int(usage.get("completion_tokens"))
    ctx_text = _format_compact_count(latest_prompt)
    if context_length > 0:
        ctx_text = f"{ctx_text}/{_format_compact_count(context_length)}"
    cost_text = _format_usage_cost(usage, model=model, base_url=base_url)
    return (
        f"{_muted('ctx')} {_style(ctx_text, '36')}  "
        f"{_muted('out')} {_style(_format_compact_count(completion), '36')}  "
        f"{_muted('cost')} {_style(cost_text, '36')}"
    )


def _model_cost_is_zero(*, model: str, base_url: str) -> bool:
    lowered_model = model.lower()
    lowered_url = base_url.lower()
    return (
        lowered_model.endswith(":free")
        or lowered_model in {"local-model", "fake", "test"}
        or "localhost" in lowered_url
        or "127.0.0.1" in lowered_url
    )


def _format_usage_cost(usage: dict[str, Any], *, model: str, base_url: str) -> str:
    if bool(usage.get("has_cost")):
        return f"${_safe_float(usage.get('cost')):.4f}"
    if _model_cost_is_zero(model=model, base_url=base_url):
        return "$0.00"
    if _safe_int(usage.get("estimated_calls")):
        return "pending"
    return "pending"


def _format_compact_count(value: Any) -> str:
    number = _safe_int(value)
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _status_dot(state: str) -> str:
    if state in {"advancing", "running", "active"}:
        return _style("●", "32")
    if state in {"paused", "queued", "planning", "idle"}:
        return _style("●", "33")
    if state in {"failed", "cancelled"}:
        return _style("●", "31")
    return _style("●", "36")
