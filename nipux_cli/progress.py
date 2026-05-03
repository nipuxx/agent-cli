"""Generic progress summaries for long-running jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProgressCheckpoint:
    message: str
    category: str
    counts: dict[str, int]
    deltas: dict[str, int]
    updates: dict[str, int]
    resolutions: dict[str, int]
    recent: str


LEDGER_KEYS = ("findings", "sources", "tasks", "experiments", "lessons", "milestones")


def build_progress_checkpoint(
    metadata: dict[str, Any],
    *,
    previous_counts: dict[str, Any] | None = None,
    step_no: int,
    tool_name: str | None,
    artifact_id: str = "",
    is_finding_output: bool = False,
) -> ProgressCheckpoint:
    """Create the operator-facing checkpoint text from durable ledger deltas."""
    counts = ledger_counts(metadata)
    previous = previous_counts or {}
    deltas = {key: counts[key] - _as_int(previous.get(key)) for key in LEDGER_KEYS}
    updates = ledger_update_counts(metadata, since=str(metadata.get("last_checkpoint_at") or ""))
    resolutions = ledger_resolution_counts(metadata, since=str(metadata.get("last_checkpoint_at") or ""))
    recent = recent_progress_bits(metadata)
    if is_finding_output:
        message = (
            f"Saved output {artifact_id}; ledgers now have {counts['findings']} findings, "
            f"{counts['sources']} sources, {counts['tasks']} tasks, and {counts['experiments']} experiments."
        )
        category = "finding"
    else:
        changed_parts = [_count_phrase(value, key, prefix="+") for key, value in deltas.items() if value > 0]
        changed_parts.extend(
            _count_phrase(value, key, prefix="~", suffix="updated") for key, value in updates.items() if value > 0
        )
        changed_parts.extend(
            _count_phrase(value, key, suffix="resolved") for key, value in resolutions.items() if value > 0
        )
        changed = ", ".join(changed_parts)
        made_progress = bool(changed)
        if not changed:
            changed = "no new durable ledger entries"
        message = (
            f"Checkpoint step #{step_no}: {changed}. Totals: {counts['findings']} findings, "
            f"{counts['sources']} sources, {counts['tasks']} tasks, {counts['experiments']} experiments, "
            f"{counts['lessons']} lessons."
        )
        category = "progress" if made_progress else "activity"
    if recent:
        message = f"{message} Recent: {recent}."
    return ProgressCheckpoint(
        message=message,
        category=category,
        counts=counts,
        deltas=deltas,
        updates=updates,
        resolutions=resolutions,
        recent=recent,
    )


def ledger_counts(metadata: dict[str, Any]) -> dict[str, int]:
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    return {
        "findings": len(_metadata_list(metadata, "finding_ledger")),
        "sources": len(_metadata_list(metadata, "source_ledger")),
        "tasks": len(_metadata_list(metadata, "task_queue")),
        "experiments": len(_metadata_list(metadata, "experiment_ledger")),
        "lessons": len(_metadata_list(metadata, "lessons")),
        "milestones": len(milestones),
    }


def ledger_update_counts(metadata: dict[str, Any], *, since: str = "") -> dict[str, int]:
    """Count durable ledger updates that do not increase ledger size."""
    counts = {key: 0 for key in LEDGER_KEYS}
    record_map = {
        "findings": "last_finding_record",
        "sources": "last_source_record",
        "tasks": "last_task_record",
        "experiments": "last_experiment_record",
    }
    for key, metadata_key in record_map.items():
        record = metadata.get(metadata_key)
        if _updated_existing_record(record, since=since):
            counts[key] += 1
    roadmap = metadata.get("last_roadmap_record")
    if isinstance(roadmap, dict) and _record_after_checkpoint(roadmap, since=since):
        updated = _as_int(roadmap.get("updated_milestones")) + _as_int(roadmap.get("updated_features"))
        added = _as_int(roadmap.get("added_milestones")) + _as_int(roadmap.get("added_features"))
        if updated > 0 and added <= 0:
            counts["milestones"] += 1
    validation = metadata.get("last_milestone_validation")
    if isinstance(validation, dict) and _record_after_checkpoint(validation, since=since):
        counts["milestones"] += 1
    return counts


def ledger_resolution_counts(metadata: dict[str, Any], *, since: str = "") -> dict[str, int]:
    """Count durable branch resolutions so task updates do not look like empty churn."""
    counts = {key: 0 for key in LEDGER_KEYS}
    task = metadata.get("last_task_record")
    if _updated_existing_record(task, since=since):
        status = str(task.get("status") or "").lower() if isinstance(task, dict) else ""
        if status in {"done", "blocked", "skipped"} and (task.get("result") or task.get("evidence_needed")):
            counts["tasks"] += 1
    experiment = metadata.get("last_experiment_record")
    if _updated_existing_record(experiment, since=since):
        status = str(experiment.get("status") or "").lower() if isinstance(experiment, dict) else ""
        if status in {"measured", "failed", "blocked", "skipped"} or experiment.get("metric_value") is not None:
            counts["experiments"] += 1
    validation = metadata.get("last_milestone_validation")
    if isinstance(validation, dict) and _record_after_checkpoint(validation, since=since):
        counts["milestones"] += 1
    return counts


def recent_progress_bits(metadata: dict[str, Any]) -> str:
    bits: list[str] = []
    findings = _metadata_list(metadata, "finding_ledger")
    if findings:
        finding = findings[-1]
        bits.append(f"finding={_clip_text(str(finding.get('name') or finding.get('title') or 'finding'), 80)}")
    active_tasks = [
        task
        for task in _metadata_list(metadata, "task_queue")
        if str(task.get("status") or "open").lower() in {"active", "open", "blocked"}
    ]
    if active_tasks:
        task = sorted(active_tasks, key=lambda entry: -_as_int(entry.get("priority")))[0]
        bits.append(f"task={_clip_text(str(task.get('title') or 'task'), 80)}")
    measured = [
        experiment
        for experiment in _metadata_list(metadata, "experiment_ledger")
        if experiment.get("metric_value") is not None
    ]
    if measured:
        experiment = measured[-1]
        metric = f"{experiment.get('metric_name') or 'metric'}={experiment.get('metric_value')}{experiment.get('metric_unit') or ''}"
        bits.append(f"measurement={_clip_text(metric, 80)}")
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    active_milestones = [
        milestone
        for milestone in milestones
        if isinstance(milestone, dict)
        and str(milestone.get("status") or "planned").lower() in {"active", "validating", "blocked"}
    ]
    if active_milestones:
        bits.append(f"milestone={_clip_text(str(active_milestones[-1].get('title') or 'milestone'), 80)}")
    return "; ".join(bits)


def _metadata_list(metadata: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _clip_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _updated_existing_record(record: Any, *, since: str) -> bool:
    return (
        isinstance(record, dict)
        and record.get("created") is False
        and _record_after_checkpoint(record, since=since)
    )


def _record_after_checkpoint(record: dict[str, Any], *, since: str) -> bool:
    if not since:
        return True
    updated_at = str(record.get("updated_at") or record.get("validated_at") or record.get("last_seen") or record.get("at") or "")
    return bool(updated_at and updated_at > since)


def _count_phrase(value: int, key: str, *, prefix: str = "", suffix: str = "") -> str:
    label = key[:-1] if value == 1 and key.endswith("s") else key
    bits = [f"{prefix}{value} {label}"]
    if suffix:
        bits.append(suffix)
    return " ".join(bits)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
