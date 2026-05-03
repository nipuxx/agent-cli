"""Reusable terminal layout primitives for Nipux frames."""

from __future__ import annotations

from typing import Any

from nipux_cli.tui_style import (
    _accent,
    _bold,
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
    del state, daemon
    title = _style("NIPUX", "38;5;123;1")
    usage_text = _token_usage_topline(token_usage or {}, context_length=context_length, model=model, base_url=base_url)
    model_line = f"{_muted('model')} {_style(_one_line(model, max(16, width // 3)), '36')}"
    if width >= 118:
        compact_model = f"{_muted('model')} {_style(_one_line(model, max(14, width // 5)), '36')}"
        return [
            _triple_line(_muted("workspace"), title, f"{compact_model}  {usage_text}", width=width),
            _muted("━" * width),
        ]
    first = _edge_line(title, model_line, width=width)
    second = _edge_line("", usage_text, width=width)
    return [
        first,
        second,
        _muted("━" * width),
    ]


def _two_col_title(left_width: int, right_width: int, left: str, right: str) -> str:
    left_title = _style(left.upper(), "38;5;252;1")
    right_title = _style(right.upper(), "38;5;252;1")
    return _fit_ansi(left_title, left_width) + _muted(" │ ") + _fit_ansi(right_title, right_width)


def _two_col_line(left: str, right: str, *, left_width: int, right_width: int) -> str:
    return _fit_ansi(left, left_width) + _muted(" │ ") + _fit_ansi(right, right_width)


def _edge_line(left: str, right: str, *, width: int) -> str:
    right_len = len(_strip_ansi(right))
    left_width = max(0, width - right_len - 2)
    left_text = _fit_ansi(left, left_width)
    gap = max(1, width - len(_strip_ansi(left_text)) - right_len)
    return _fit_ansi(left_text + " " * gap + right, width)


def _triple_line(left: str, center: str, right: str, *, width: int) -> str:
    right_len = len(_strip_ansi(right))
    center_len = len(_strip_ansi(center))
    left_len = len(_strip_ansi(left))
    center_start = max(left_len + 2, (width - center_len) // 2)
    right_start = max(center_start + center_len + 1, width - right_len)
    if right_start >= width:
        return _edge_line(center, right, width=width)
    parts = [
        left,
        " " * max(1, center_start - left_len),
        center,
        " " * max(1, right_start - center_start - center_len),
        right,
    ]
    return _fit_ansi("".join(parts), width)


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
    hint = _muted(hint or "Enter send  ·  / commands  ·  arrows navigate")
    label = _accent(prompt_label) if prompt_label == "❯" else _muted(prompt_label)
    prompt = f"{label} {visible_input}{_accent('▌')}"
    lines = []
    if suggestions:
        lines.extend(suggestions)
    title = " message "
    lines.extend([
        _muted("╭─" + title + "─" * max(0, width - len(title) - 2)),
        _fit_ansi(_muted("│ ") + prompt, width),
        _fit_ansi(_muted("╰─ ") + hint, width),
    ])
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
        return (
            f"{_muted('ctx')} {_style('0', '36')}  "
            f"{_muted('out')} {_style('0', '36')}  "
            f"{_muted('tok')} {_style('0', '36')}  "
            f"{_muted('cost')} {_style('$0.00', '36')}"
        )
    latest_prompt = _safe_int(usage.get("latest_prompt_tokens"))
    completion = _safe_int(usage.get("completion_tokens"))
    total = _safe_int(usage.get("total_tokens")) or latest_prompt + completion
    ctx_text = _format_compact_count(latest_prompt)
    if context_length > 0:
        ctx_text = f"{ctx_text}/{_format_compact_count(context_length)}"
    cost_text = _format_usage_cost(usage, model=model, base_url=base_url)
    return (
        f"{_muted('ctx')} {_style(ctx_text, '36')}  "
        f"{_muted('out')} {_style(_format_compact_count(completion), '36')}  "
        f"{_muted('tok')} {_style(_format_compact_count(total), '36')}  "
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
    input_rate = _safe_optional_float(usage.get("input_cost_per_million"))
    output_rate = _safe_optional_float(usage.get("output_cost_per_million"))
    if input_rate is not None and output_rate is not None:
        prompt = _safe_int(usage.get("prompt_tokens"))
        completion = _safe_int(usage.get("completion_tokens"))
        if prompt > 0 or completion > 0:
            estimated = (prompt / 1_000_000 * input_rate) + (completion / 1_000_000 * output_rate)
            return f"~${estimated:.4f}"
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


def _safe_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_dot(state: str) -> str:
    if state in {"advancing", "running", "active"}:
        return _style("●", "32")
    if state in {"paused", "queued", "planning", "idle"}:
        return _style("●", "33")
    if state in {"failed", "cancelled"}:
        return _style("●", "31")
    return _style("●", "36")
