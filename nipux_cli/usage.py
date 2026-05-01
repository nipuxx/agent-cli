"""Formatting helpers for model token and cost usage."""

from __future__ import annotations

from typing import Any

from nipux_cli.tui_layout import _format_compact_count, _format_usage_cost


def format_usage_report(
    *,
    title: str,
    usage: dict[str, Any],
    context_length: int,
    model: str,
    base_url: str,
) -> list[str]:
    calls = _safe_int(usage.get("calls"))
    prompt = _safe_int(usage.get("prompt_tokens"))
    completion = _safe_int(usage.get("completion_tokens"))
    total = _safe_int(usage.get("total_tokens")) or prompt + completion
    latest_prompt = _safe_int(usage.get("latest_prompt_tokens"))
    latest_completion = _safe_int(usage.get("latest_completion_tokens"))
    latest_total = _safe_int(usage.get("latest_total_tokens")) or latest_prompt + latest_completion
    estimated = _safe_int(usage.get("estimated_calls"))
    cached = _safe_int(usage.get("cached_tokens"))
    reasoning = _safe_int(usage.get("reasoning_tokens"))
    cost = _format_usage_cost(usage, model=model, base_url=base_url)
    context_text = _format_compact_count(latest_prompt)
    if context_length > 0:
        context_text = f"{context_text}/{_format_compact_count(context_length)}"
    lines = [
        f"usage {title}",
        "=" * 80,
        f"model: {model}",
        f"calls: {calls} | estimated: {estimated}",
        f"tokens: total={_format_compact_count(total)} prompt={_format_compact_count(prompt)} output={_format_compact_count(completion)}",
        f"latest: ctx={context_text} output={_format_compact_count(latest_completion)} total={_format_compact_count(latest_total)}",
        f"details: cached={_format_compact_count(cached)} reasoning={_format_compact_count(reasoning)} cost={cost}",
    ]
    if calls <= 0:
        lines.append("no model usage has been recorded for this job yet")
    elif estimated:
        lines.append("some usage is estimated because the provider did not return complete token/cost metadata")
    elif not bool(usage.get("has_cost")):
        lines.append("cost is pending unless the provider returns cost metadata or the model is configured as local/free")
    return lines


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
