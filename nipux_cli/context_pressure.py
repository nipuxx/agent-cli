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
USAGE_TOKEN_BANDS = (
    (20_000_000, "critical"),
    (5_000_000, "high"),
    (1_000_000, "watch"),
)
USAGE_CALL_BANDS = (
    (2_000, "critical"),
    (1_000, "high"),
    (200, "watch"),
)
USAGE_COST_BANDS = (
    (10.0, "critical"),
    (5.0, "high"),
    (1.0, "watch"),
)
USAGE_BAND_RANK = {"": 0, "watch": 1, "high": 2, "critical": 3}


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


def usage_pressure_for_prompt(job: dict[str, Any], usage: dict[str, Any] | None) -> str:
    usage = usage if isinstance(usage, dict) else {}
    band = _usage_pressure_band(usage)
    if not band:
        return "None."
    calls = _as_int(usage.get("calls"))
    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    completion_tokens = _as_int(usage.get("completion_tokens"))
    total_tokens = _as_int(usage.get("total_tokens")) or prompt_tokens + completion_tokens
    latest_prompt_tokens = _as_int(usage.get("latest_prompt_tokens"))
    latest_context_length = _as_int(usage.get("latest_context_length"))
    durable_records = _durable_usage_signal_count(job)
    tokens_per_record = total_tokens / max(1, durable_records)
    latest_context = compact_token_count(latest_prompt_tokens)
    if latest_context_length:
        latest_context = f"{latest_context}/{compact_token_count(latest_context_length)}"
    bits = [
        f"calls={calls}",
        f"tokens={compact_token_count(total_tokens)}",
        f"prompt={compact_token_count(prompt_tokens)}",
        f"output={compact_token_count(completion_tokens)}",
    ]
    if bool(usage.get("has_cost")):
        bits.append(f"cost=${_as_float(usage.get('cost')):.4f}")
    if latest_prompt_tokens:
        bits.append(f"latest_context={latest_context}")
    lines = [
        f"Cumulative model usage pressure is {band}: " + " ".join(bits) + ".",
        (
            f"Durable progress records={durable_records}; "
            f"approximately {compact_token_count(int(tokens_per_record))} tokens per durable record."
        ),
        (
            "Next action should be high leverage: execute, measure, validate, consolidate, defer, or mark a branch "
            "blocked/skipped from concrete evidence. Avoid low-yield retries, broad rereads, or new research unless it "
            "directly resolves an active contract or unlocks the next experiment."
        ),
    ]
    return "\n".join(lines)


def emit_usage_pressure_update(db: AgentDB, job_id: str, usage: dict[str, Any]) -> None:
    band = _usage_pressure_band(usage)
    if not band:
        return
    job = db.get_job(job_id)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    previous = metadata.get("usage_pressure") if isinstance(metadata.get("usage_pressure"), dict) else {}
    previous_band = str(previous.get("band") or "")
    total_tokens = _as_int(usage.get("total_tokens"))
    previous_high_tokens = _as_int(previous.get("high_water_tokens"))
    should_emit = (
        previous_band != band
        or (previous_high_tokens > 0 and total_tokens >= int(previous_high_tokens * 1.5))
        or (previous_high_tokens <= 0)
    )
    pressure = {
        "band": band,
        "calls": _as_int(usage.get("calls")),
        "total_tokens": total_tokens,
        "prompt_tokens": _as_int(usage.get("prompt_tokens")),
        "completion_tokens": _as_int(usage.get("completion_tokens")),
        "cost": _as_float(usage.get("cost")) if bool(usage.get("has_cost")) else None,
        "has_cost": bool(usage.get("has_cost")),
        "high_water_tokens": max(total_tokens, previous_high_tokens),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    db.update_job_metadata(job_id, {"usage_pressure": pressure})
    if not should_emit:
        return
    cost_text = ""
    if pressure["has_cost"]:
        cost_text = f" cost=${pressure['cost']:.4f}"
    db.append_agent_update(
        job_id,
        (
            f"Usage pressure {band}: {compact_token_count(total_tokens)} tokens across "
            f"{pressure['calls']} model calls.{cost_text} Prefer high-leverage actions, measurement, "
            "consolidation, or explicit blocked/deferred branches over low-yield churn."
        ),
        category="update",
        metadata={"kind": "usage_pressure", "usage_pressure": pressure},
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


def _usage_pressure_band(usage: dict[str, Any]) -> str:
    total_tokens = _as_int(usage.get("total_tokens"))
    if total_tokens <= 0:
        total_tokens = _as_int(usage.get("prompt_tokens")) + _as_int(usage.get("completion_tokens"))
    calls = _as_int(usage.get("calls"))
    cost = _as_float(usage.get("cost")) if bool(usage.get("has_cost")) else 0.0
    band = ""
    for threshold, candidate in USAGE_TOKEN_BANDS:
        if total_tokens >= threshold and USAGE_BAND_RANK[candidate] > USAGE_BAND_RANK[band]:
            band = candidate
            break
    for threshold, candidate in USAGE_CALL_BANDS:
        if calls >= threshold and USAGE_BAND_RANK[candidate] > USAGE_BAND_RANK[band]:
            band = candidate
            break
    if bool(usage.get("has_cost")):
        for threshold, candidate in USAGE_COST_BANDS:
            if cost >= threshold and USAGE_BAND_RANK[candidate] > USAGE_BAND_RANK[band]:
                band = candidate
                break
    return band


def _context_pressure_band(fraction: float) -> str:
    for threshold, band in CONTEXT_PRESSURE_BANDS:
        if fraction >= threshold:
            return band
    return ""


def _durable_usage_signal_count(job: dict[str, Any]) -> int:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    count = 0
    for key in ("finding_ledger", "source_ledger", "experiment_ledger", "lessons"):
        records = metadata.get(key)
        if isinstance(records, list):
            count += sum(1 for record in records if isinstance(record, dict))
    tasks = metadata.get("task_queue")
    if isinstance(tasks, list):
        count += sum(
            1
            for task in tasks
            if isinstance(task, dict)
            and str(task.get("status") or "open").lower() in {"done", "blocked", "skipped"}
            and (task.get("result") or task.get("evidence_needed") or task.get("acceptance_criteria"))
        )
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    count += sum(
        1
        for milestone in milestones
        if isinstance(milestone, dict)
        and str(milestone.get("status") or "planned").lower() in {"active", "validating", "done", "blocked", "skipped"}
    )
    return count


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
