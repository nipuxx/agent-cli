"""Deterministic rolling memory summaries for long-running jobs."""

from __future__ import annotations

from nipux_cli.db import AgentDB
from nipux_cli.operator_context import active_prompt_operator_entries


def _clip_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def refresh_memory_index(db: AgentDB, job_id: str, *, max_steps: int = 8, max_artifacts: int = 8) -> str:
    """Write a compact, artifact-referenced job memory entry.

    This is deliberately deterministic. A local model can later improve the
    prose, but the daemon should always have a cheap compaction path that runs
    after every step and survives model failures.
    """

    job = db.get_job(job_id)
    steps = db.list_steps(job_id=job_id)[-max_steps:]
    artifacts = db.list_artifacts(job_id, limit=max_artifacts)
    artifact_refs = [artifact["id"] for artifact in artifacts]
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    operator_messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    active_operator = [
        entry
        for entry in active_prompt_operator_entries(operator_messages)
        if str(entry.get("mode") or "steer") in {"steer", "follow_up"}
    ][-5:]
    operator_notes = [
        entry for entry in operator_messages
        if isinstance(entry, dict)
        and str(entry.get("mode") or "steer") == "note"
    ][-3:]

    lines = [
        f"Job lifecycle status: {job['status']}",
        f"Objective: {job['objective']}",
        "",
        "Active operator context:",
    ]
    if not active_operator and not operator_notes:
        lines.append("- none")
    for entry in active_operator:
        lines.append(
            f"- {entry.get('mode') or 'steer'} {entry.get('event_id') or ''}: "
            f"{_clip_text(entry.get('message') or '', 300)}"
        )
    for entry in operator_notes:
        lines.append(f"- note {entry.get('event_id') or ''}: {_clip_text(entry.get('message') or '', 300)}")

    lines.extend([
        "",
        "Recent steps:",
    ])
    if not steps:
        lines.append("- none")
    for step in steps:
        tool = f" tool={step['tool_name']}" if step.get("tool_name") else ""
        summary = step.get("summary") or step.get("error") or ""
        lines.append(f"- #{step['step_no']} {step['kind']} {step['status']}{tool}: {_clip_text(summary, 280)}")

    lines.extend(["", "Recent artifacts:"])
    if not artifacts:
        lines.append("- none")
    for artifact in artifacts:
        title = artifact.get("title") or artifact["id"]
        summary = artifact.get("summary") or ""
        lines.append(f"- {artifact['id']} {_clip_text(title, 120)} ({artifact['type']}): {_clip_text(summary, 240)}")

    tasks = _metadata_list(metadata, "task_queue")
    findings = _metadata_list(metadata, "finding_ledger")
    sources = _metadata_list(metadata, "source_ledger")
    experiments = _metadata_list(metadata, "experiment_ledger")
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}

    lines.extend(["", "Durable progress ledgers:"])
    lines.append(
        "- "
        + ", ".join(
            [
                f"tasks={len(tasks)}",
                f"findings={len(findings)}",
                f"sources={len(sources)}",
                f"experiments={len(experiments)}",
                f"roadmap={'yes' if roadmap else 'no'}",
            ]
        )
    )
    for task in _rank_tasks(tasks)[:4]:
        lines.append(
            "- task "
            f"{task.get('status') or 'open'} "
            f"{_clip_text(task.get('title') or '', 120)} "
            f"contract={task.get('output_contract') or '?'}"
        )
    for experiment in experiments[-3:]:
        metric = ""
        if experiment.get("metric_value") not in (None, ""):
            metric = (
                f" metric={experiment.get('metric_name') or 'value'}="
                f"{experiment.get('metric_value')}{experiment.get('metric_unit') or ''}"
            )
        lines.append(
            "- experiment "
            f"{experiment.get('status') or 'planned'} "
            f"{_clip_text(experiment.get('title') or '', 120)}{metric}"
        )
    for finding in findings[-3:]:
        lines.append(f"- finding {_clip_text(finding.get('name') or finding.get('title') or '', 140)}")
    for source in sources[-3:]:
        score = source.get("usefulness_score")
        lines.append(f"- source {_clip_text(source.get('source') or '', 140)} score={score if score is not None else '?'}")
    if roadmap:
        lines.append(
            "- roadmap "
            f"{roadmap.get('status') or 'planned'} "
            f"{_clip_text(roadmap.get('title') or 'Roadmap', 140)} "
            f"current={_clip_text(roadmap.get('current_milestone') or '', 120)}"
        )

    return db.upsert_memory(
        job_id=job_id,
        key="rolling_state",
        summary="\n".join(lines).strip(),
        artifact_refs=artifact_refs,
    )


def _metadata_list(metadata: dict, key: str) -> list[dict]:
    values = metadata.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _rank_tasks(tasks: list[dict]) -> list[dict]:
    status_rank = {"active": 0, "open": 1, "blocked": 2, "validating": 3, "done": 4, "skipped": 5}
    return sorted(
        tasks,
        key=lambda task: (
            status_rank.get(str(task.get("status") or "open"), 9),
            -int(task.get("priority") or 0),
            str(task.get("title") or ""),
        ),
    )
