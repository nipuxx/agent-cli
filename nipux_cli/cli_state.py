"""Persistent CLI focus state and job lookup helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nipux_cli.config import AppConfig, load_config
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


def setup_completed() -> bool:
    return bool(read_shell_state().get("setup_completed"))


def mark_setup_completed() -> None:
    write_shell_state({"setup_completed": True})


def model_setup_fingerprint(config: AppConfig | None = None) -> str:
    config = config or load_config()
    key_hash = hashlib.sha256(config.model.api_key.encode("utf-8")).hexdigest() if config.model.api_key else ""
    payload = {
        "model": config.model.model,
        "base_url": config.model.base_url,
        "api_key_env": config.model.api_key_env,
        "api_key_hash": key_hash,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def model_setup_verified(config: AppConfig | None = None) -> bool:
    state = read_shell_state()
    marker = state.get("model_setup_verified")
    if not isinstance(marker, dict) or not marker.get("ok"):
        return False
    return marker.get("fingerprint") == model_setup_fingerprint(config)


def mark_model_setup_verified(config: AppConfig | None = None) -> None:
    config = config or load_config()
    write_shell_state(
        {
            "setup_completed": True,
            "model_setup_verified": {
                "ok": True,
                "fingerprint": model_setup_fingerprint(config),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "model": config.model.model,
                "base_url": config.model.base_url,
                "api_key_env": config.model.api_key_env,
            },
        }
    )


def clear_model_setup_verified() -> None:
    write_shell_state({"model_setup_verified": {}})
