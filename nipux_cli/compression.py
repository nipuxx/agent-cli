"""Deterministic rolling memory summaries for long-running jobs."""

from __future__ import annotations

from nipux_cli.db import AgentDB


def refresh_memory_index(db: AgentDB, job_id: str, *, max_steps: int = 20, max_artifacts: int = 20) -> str:
    """Write a compact, artifact-referenced job memory entry.

    This is deliberately deterministic. A local model can later improve the
    prose, but the daemon should always have a cheap compaction path that runs
    after every step and survives model failures.
    """

    job = db.get_job(job_id)
    steps = db.list_steps(job_id=job_id)[-max_steps:]
    artifacts = db.list_artifacts(job_id, limit=max_artifacts)
    artifact_refs = [artifact["id"] for artifact in artifacts]

    lines = [
        f"Job lifecycle status: {job['status']}",
        f"Objective: {job['objective']}",
        "",
        "Recent steps:",
    ]
    if not steps:
        lines.append("- none")
    for step in steps:
        tool = f" tool={step['tool_name']}" if step.get("tool_name") else ""
        summary = step.get("summary") or step.get("error") or ""
        lines.append(f"- #{step['step_no']} {step['kind']} {step['status']}{tool}: {summary}")

    lines.extend(["", "Recent artifacts:"])
    if not artifacts:
        lines.append("- none")
    for artifact in artifacts:
        title = artifact.get("title") or artifact["id"]
        summary = artifact.get("summary") or ""
        lines.append(f"- {artifact['id']} {title} ({artifact['type']}): {summary}")

    return db.upsert_memory(
        job_id=job_id,
        key="rolling_state",
        summary="\n".join(lines).strip(),
        artifact_refs=artifact_refs,
    )
