"""Runtime checks for the barebones agent."""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nipux_cli.config import AppConfig, load_config
from nipux_cli.db import AgentDB
from nipux_cli.tools import DEFAULT_REGISTRY


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _check_writable_dir(path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check("state_dir_writable", True, str(path))
    except OSError as exc:
        return Check("state_dir_writable", False, f"{path}: {exc}")


def _check_db(config: AppConfig) -> Check:
    try:
        db = AgentDB(config.runtime.state_db_path)
        db.close()
        return Check("sqlite", True, str(config.runtime.state_db_path))
    except Exception as exc:
        return Check("sqlite", False, str(exc))


def _check_tool_surface() -> Check:
    names = DEFAULT_REGISTRY.names()
    forbidden = sorted({"terminal", "delegate_task", "skill_manage", "image_generate"} & set(names))
    if forbidden:
        return Check("tool_surface", False, f"forbidden tools exposed: {', '.join(forbidden)}")
    return Check("tool_surface", True, f"{len(names)} tools: {', '.join(names)}")


def _check_browser_runtime() -> Check:
    direct = shutil.which("agent-browser")
    if direct:
        return Check("browser_runtime", True, f"agent-browser: {direct}")
    npx = shutil.which("npx")
    if npx:
        return Check("browser_runtime", True, f"agent-browser available through npx fallback: {npx}")
    return Check(
        "browser_runtime",
        False,
        "agent-browser not found and npx is unavailable; install with: npm install -g agent-browser && agent-browser install",
    )


def _check_model_endpoint(config: AppConfig) -> Check:
    url = config.model.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.model.api_key or 'local-no-key'}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = response.read(2048).decode("utf-8", errors="replace")
        try:
            data = json.loads(payload)
            count = len(data.get("data", [])) if isinstance(data, dict) else "unknown"
            return Check("model_endpoint", True, f"{url} returned models={count}")
        except json.JSONDecodeError:
            return Check("model_endpoint", True, f"{url} responded")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Check("model_endpoint", False, f"{url}: {exc}")


def run_doctor(*, config: AppConfig | None = None, check_model: bool = False) -> list[Check]:
    config = config or load_config()
    checks = [
        _check_writable_dir(config.runtime.home),
        _check_db(config),
        _check_tool_surface(),
        _check_browser_runtime(),
    ]
    if check_model:
        checks.append(_check_model_endpoint(config))
    return checks
