"""Digest rendering and optional email delivery."""

from __future__ import annotations

import smtplib
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from nipux_cli.config import AppConfig, EmailConfig
from nipux_cli.db import AgentDB


def _metadata_list(job: dict, key: str) -> list[dict]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    values = metadata.get(key)
    return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []


def render_job_digest(db: AgentDB, job_id: str) -> str:
    job = db.get_job(job_id)
    artifacts = db.list_artifacts(job_id, limit=50)
    steps = db.list_steps(job_id=job_id)
    findings = _metadata_list(job, "finding_ledger")
    sources = _metadata_list(job, "source_ledger")
    tasks = _metadata_list(job, "task_queue")
    lessons = _metadata_list(job, "lessons")
    reflections = _metadata_list(job, "reflections")
    lines = [
        f"# {job['title']}",
        "",
        f"Status: {job['status']}",
        f"Findings: {len(findings)}",
        f"Sources: {len(sources)}",
        f"Tasks: {len(tasks)}",
        f"Lessons: {len(lessons)}",
        "",
        "## Objective",
        "",
        job["objective"],
        "",
        "## Recent Steps",
        "",
    ]
    if not steps:
        lines.append("- No steps have run yet.")
    for step in steps[-20:]:
        tool = f" `{step['tool_name']}`" if step.get("tool_name") else ""
        lines.append(f"- #{step['step_no']} {step['kind']}{tool}: {step['status']} - {step.get('summary') or ''}")
    lines.extend(["", "## Best Findings", ""])
    if not findings:
        lines.append("- No findings recorded yet.")
    for finding in sorted(findings, key=lambda item: float(item.get("score") or 0), reverse=True)[:15]:
        details = " | ".join(str(finding.get(key) or "") for key in ("category", "location", "contact") if finding.get(key))
        suffix = f" - {details}" if details else ""
        lines.append(f"- {finding.get('name') or 'unknown'} (score={finding.get('score')}){suffix}")
        if finding.get("reason"):
            lines.append(f"  - {finding['reason']}")
    lines.extend(["", "## Source Learning", ""])
    if not sources:
        lines.append("- No sources scored yet.")
    for source in sorted(sources, key=lambda item: float(item.get("usefulness_score") or 0), reverse=True)[:12]:
        lines.append(
            f"- {source.get('source')} score={source.get('usefulness_score')} "
            f"findings={source.get('yield_count') or 0} fails={source.get('fail_count') or 0}: {source.get('last_outcome') or ''}"
        )
    lines.extend(["", "## Task Queue", ""])
    if not tasks:
        lines.append("- No tasks recorded yet.")
    status_order = {"active": 0, "open": 1, "blocked": 2, "done": 3, "skipped": 4}
    for task in sorted(tasks, key=lambda item: (status_order.get(str(item.get("status") or "open"), 9), -int(item.get("priority") or 0)))[:15]:
        lines.append(f"- {task.get('status') or 'open'} p={task.get('priority') or 0}: {task.get('title') or 'untitled'}")
        if task.get("result"):
            lines.append(f"  - {task['result']}")
    lines.extend(["", "## Lessons", ""])
    if not lessons:
        lines.append("- No lessons recorded yet.")
    for lesson in lessons[-12:]:
        lines.append(f"- {lesson.get('category') or 'memory'}: {lesson.get('lesson') or ''}")
    if reflections:
        lines.extend(["", "## Current Strategy", ""])
        reflection = reflections[-1]
        lines.append(reflection.get("summary") or "")
        if reflection.get("strategy"):
            lines.append("")
            lines.append(reflection["strategy"])
    lines.extend(["", "## Artifacts", ""])
    if not artifacts:
        lines.append("- No artifacts yet.")
    for artifact in artifacts[:20]:
        title = artifact.get("title") or artifact["id"]
        lines.append(f"- {title} ({artifact['type']}): {artifact['path']}")
    return "\n".join(lines).rstrip() + "\n"


def send_digest_email(config: EmailConfig, *, subject: str, body: str, to_addr: str | None = None) -> dict:
    if not config.enabled:
        return {"sent": False, "dry_run": True, "reason": "email.disabled", "subject": subject, "body": body}
    target = to_addr or config.to_addr
    if not all([config.smtp_host, config.from_addr, target]):
        raise ValueError("Email is enabled but smtp_host/from_addr/to_addr is incomplete")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.from_addr
    message["To"] = target
    message.set_content(body)
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        if config.use_tls:
            smtp.starttls()
        if config.username:
            smtp.login(config.username, config.password)
        smtp.send_message(message)
    return {"sent": True, "target": target, "subject": subject}


def render_daily_digest(db: AgentDB) -> str:
    jobs = [job for job in db.list_jobs() if job["status"] not in {"cancelled"}]
    lines = ["# Nipux CLI Daily Digest", ""]
    if not jobs:
        lines.append("No jobs are currently tracked.")
        return "\n".join(lines).rstrip() + "\n"

    for job in jobs:
        artifacts = db.list_artifacts(job["id"], limit=10)
        steps = db.list_steps(job_id=job["id"])[-5:]
        findings = _metadata_list(job, "finding_ledger")
        sources = _metadata_list(job, "source_ledger")
        tasks = _metadata_list(job, "task_queue")
        lessons = _metadata_list(job, "lessons")
        reflections = _metadata_list(job, "reflections")
        finding_batches = [artifact for artifact in artifacts if "finding" in str(artifact.get("title") or artifact.get("summary") or "").lower()]
        lines.extend([
            f"## {job['title']}",
            "",
            f"Status: {job['status']}",
            f"Kind: {job['kind']}",
            f"Counts: {len(findings)} findings, {len(sources)} sources, {len(tasks)} tasks, {len(lessons)} lessons, {len(finding_batches)} recent finding artifacts",
            "",
            "Recent steps:",
        ])
        if not steps:
            lines.append("- none")
        for step in steps:
            tool = f" `{step['tool_name']}`" if step.get("tool_name") else ""
            lines.append(f"- #{step['step_no']} {step['kind']}{tool}: {step['status']} - {step.get('summary') or ''}")
        lines.extend(["", "Best findings:"])
        if not findings:
            lines.append("- none")
        for finding in sorted(findings, key=lambda item: float(item.get("score") or 0), reverse=True)[:8]:
            lines.append(f"- {finding.get('name') or 'unknown'} (score={finding.get('score')}) - {finding.get('reason') or finding.get('category') or ''}")
        lines.extend(["", "Task queue:"])
        if not tasks:
            lines.append("- none")
        status_order = {"active": 0, "open": 1, "blocked": 2, "done": 3, "skipped": 4}
        for task in sorted(tasks, key=lambda item: (status_order.get(str(item.get("status") or "open"), 9), -int(item.get("priority") or 0)))[:8]:
            lines.append(f"- {task.get('status') or 'open'} p={task.get('priority') or 0}: {task.get('title') or 'untitled'}")
        lines.extend(["", "Lessons learned:"])
        if not lessons:
            lines.append("- none")
        for lesson in lessons[-8:]:
            lines.append(f"- {lesson.get('category') or 'memory'}: {lesson.get('lesson') or ''}")
        lines.extend(["", "Source quality:"])
        if not sources:
            lines.append("- none")
        for source in sorted(sources, key=lambda item: float(item.get("usefulness_score") or 0), reverse=True)[:8]:
            lines.append(f"- {source.get('source')} score={source.get('usefulness_score')} findings={source.get('yield_count') or 0}: {source.get('last_outcome') or ''}")
        if reflections:
            reflection = reflections[-1]
            lines.extend(["", "Current strategy:", f"- {reflection.get('strategy') or reflection.get('summary') or ''}"])
        lines.extend(["", "Next branches:"])
        lines.append("- Continue with high-yield source types, avoid low-yield paths, and save durable findings as artifacts.")
        lines.extend(["", "Recent artifacts:"])
        if not artifacts:
            lines.append("- none")
        for artifact in artifacts:
            title = artifact.get("title") or artifact["id"]
            lines.append(f"- {title} ({artifact['type']}): {artifact['path']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_daily_digest(config: AppConfig, db: AgentDB, *, day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    target = config.email.to_addr or "dry-run"
    subject = f"Nipux CLI daily digest - {day}"
    if db.digest_exists(day=day, target=target):
        return {"sent": False, "skipped": True, "reason": "already_recorded", "day": day, "target": target}

    body = render_daily_digest(db)
    config.runtime.digests_dir.mkdir(parents=True, exist_ok=True)
    body_path = Path(config.runtime.digests_dir) / f"{day}-daily.md"
    body_path.write_text(body, encoding="utf-8")

    try:
        email_result = send_digest_email(config.email, subject=subject, body=body)
        status = "sent" if email_result.get("sent") else "dry_run"
        digest_id = db.record_digest(day=day, target=target, subject=subject, body_path=body_path, status=status)
        return {
            "digest_id": digest_id,
            "status": status,
            "day": day,
            "target": target,
            "path": str(body_path),
            "email": email_result,
        }
    except Exception as exc:
        digest_id = db.record_digest(
            day=day,
            target=target,
            subject=subject,
            body_path=body_path,
            status="failed",
            error=str(exc),
        )
        return {"digest_id": digest_id, "status": "failed", "day": day, "target": target, "path": str(body_path), "error": str(exc)}
