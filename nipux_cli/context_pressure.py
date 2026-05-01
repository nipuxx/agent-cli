"""Context-pressure signals for long-running worker prompts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from nipux_cli.db import AgentDB


CONTEXT_PRESSURE_BANDS = (
    (0.95, "critical"),
    (0.85, "high"),
    (0.65, "watch"),
)


def context_pressure_for_prompt(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    pressure = metadata.get("context_pressure") if isinstance(metadata.get("context_pressure"), dict) else {}
    band = str(pressure.get("band") or "")
    if band not in {"watch", "high", "critical"}:
        return "None."
    prompt_tokens = compact_token_count(pressure.get("prompt_tokens"))
    context_length = compact_token_count(pressure.get("context_length"))
    context_text = prompt_tokens
    if context_length != "0":
        context_text = f"{context_text}/{context_length}"
    fraction = _as_float(pressure.get("fraction"))
    fraction_text = f" ({fraction:.0%})" if fraction else ""
    return (
        f"Context pressure is {band}: latest prompt used {context_text}{fraction_text}. "
        "Keep the next turn compact; prefer durable memory, ledgers, artifact references, and explicit decisions "
        "over copying raw history."
    )


def emit_context_pressure_update(db: AgentDB, job_id: str, usage: dict[str, Any]) -> None:
    fraction = _as_float(usage.get("context_fraction"))
    band = _context_pressure_band(fraction)
    if not band:
        return
    job = db.get_job(job_id)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    previous = metadata.get("context_pressure") if isinstance(metadata.get("context_pressure"), dict) else {}
    previous_band = str(previous.get("band") or "")
    previous_high = _as_float(previous.get("high_water_fraction"))
    should_emit = previous_band != band or fraction >= previous_high + 0.10
    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    context_length = _as_int(usage.get("context_length"))
    pressure = {
        "band": band,
        "fraction": round(fraction, 6),
        "high_water_fraction": round(max(fraction, previous_high), 6),
        "prompt_tokens": prompt_tokens,
        "context_length": context_length,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    db.update_job_metadata(job_id, {"context_pressure": pressure})
    if not should_emit:
        return
    denominator = f"/{compact_token_count(context_length)}" if context_length else ""
    estimated = ", estimated" if usage.get("estimated") else ""
    db.append_agent_update(
        job_id,
        (
            f"Context pressure {band}: latest prompt "
            f"{compact_token_count(prompt_tokens)}{denominator} ({fraction:.0%}{estimated}). "
            "Prefer compact memory, ledgers, artifact references, and explicit decisions over raw history."
        ),
        category="update",
        metadata={"kind": "context_pressure", "context_pressure": pressure},
    )


def compact_token_count(value: object) -> str:
    number = _as_int(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


def _context_pressure_band(fraction: float) -> str:
    for threshold, band in CONTEXT_PRESSURE_BANDS:
        if fraction >= threshold:
            return band
    return ""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
