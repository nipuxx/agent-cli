"""Persistent CLI focus state and job lookup helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nipux_cli.config import load_config
from nipux_cli.db import AgentDB


def default_job_id(db: AgentDB) -> str | None:
    configured = configured_focus_job_id(db)
    if configured:
        return configured
    jobs = db.list_jobs()
    for status in ("running", "queued", "planning", "paused", "failed", "completed"):
        for job in jobs:
            if job.get("status") == status:
                return str(job["id"])
    return str(jobs[0]["id"]) if jobs else None


def configured_focus_job_id(db: AgentDB) -> str | None:
    job_id = read_shell_state().get("focus_job_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    try:
        db.get_job(job_id)
    except KeyError:
        return None
    return job_id


def find_job(db: AgentDB, query: str) -> dict[str, Any] | None:
    needle = " ".join(query.split()).lower()
    if not needle:
        return None
    jobs = db.list_jobs()
    for job in jobs:
        if str(job["id"]).lower() == needle:
            return job
    for job in jobs:
        if str(job.get("title") or "").lower() == needle:
            return job
    for job in jobs:
        if needle in str(job.get("title") or "").lower():
            return job
    return None


def shell_state_path() -> Path:
    config = load_config()
    config.ensure_dirs()
    return config.runtime.home / "shell_state.json"


def read_shell_state() -> dict[str, Any]:
    path = shell_state_path()
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_shell_state(patch: dict[str, Any]) -> None:
    state = read_shell_state()
    state.update(patch)
    shell_state_path().write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
