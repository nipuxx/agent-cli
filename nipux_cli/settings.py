"""Inline config editing helpers for Nipux slash commands."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from nipux_cli.config import default_config_yaml, get_agent_home, load_config
from nipux_cli.tui_commands import SETTINGS_FIELD_TYPES


def config_field_value(field: str, config: Any | None = None) -> Any:
    config = load_config() if config is None else config
    values = {
        "model.name": config.model.model,
        "model.base_url": config.model.base_url,
        "model.api_key_env": config.model.api_key_env,
        "model.context_length": config.model.context_length,
        "model.request_timeout_seconds": config.model.request_timeout_seconds,
        "model.input_cost_per_million": config.model.input_cost_per_million,
        "model.output_cost_per_million": config.model.output_cost_per_million,
        "runtime.home": str(config.runtime.home),
        "runtime.max_step_seconds": config.runtime.max_step_seconds,
        "runtime.artifact_inline_char_limit": config.runtime.artifact_inline_char_limit,
        "runtime.daily_digest_enabled": config.runtime.daily_digest_enabled,
        "runtime.daily_digest_time": config.runtime.daily_digest_time,
    }
    return values.get(field, "")


def save_config_field(field: str, raw_value: str) -> Any:
    value = _coerce_config_value(field, raw_value)
    data = _load_config_yaml()
    section, key = field.split(".", 1)
    target = data.setdefault(section, {})
    if not isinstance(target, dict):
        target = {}
        data[section] = target
    target[key] = value
    _save_config_yaml(data)
    return value


def inline_setting_notice(field: str, raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return f"kept {field}"
    if field == "secret:model.api_key":
        config = load_config()
        name = config.model.api_key_env
        _save_env_secret(name, value)
        return f"saved {name} in {_short_path(get_agent_home() / '.env')}"
    try:
        saved = save_config_field(field, value)
    except ValueError as exc:
        return f"{field}: {exc}"
    return f"saved {field} = {saved}"


def edit_target_label(field: str) -> str:
    if field == "secret:model.api_key":
        return "API key"
    return field


def edit_target_hint(field: str, config: Any | None = None) -> str:
    config = config or load_config()
    if field == "secret:model.api_key":
        state = "set" if config.model.api_key else "missing"
        return f"Editing API key ({state}). Enter saves, Esc cancels. Input is hidden."
    current = config_field_value(field, config)
    return f"Editing {field}. Current: {current}. Enter saves, Esc cancels, empty keeps current."


def edit_target_masks_input(field: str | None) -> bool:
    return field == "secret:model.api_key"


def _config_path() -> Path:
    return get_agent_home() / "config.yaml"


def _load_config_yaml() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        loaded = yaml.safe_load(default_config_yaml()) or {}
        return loaded if isinstance(loaded, dict) else {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _save_config_yaml(data: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _save_env_secret(name: str, value: str) -> None:
    env_path = get_agent_home() / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" not in raw or raw.strip().startswith("#"):
                continue
            key, current = raw.split("=", 1)
            if key.strip():
                existing[key.strip()] = current.strip()
    existing[name] = value
    env_path.write_text("\n".join(f"{key}={current}" for key, current in existing.items()) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    os.environ[name] = value


def _coerce_config_value(field: str, raw_value: str) -> Any:
    kind = SETTINGS_FIELD_TYPES.get(field, "str")
    value = raw_value.strip()
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError("use true or false")
    if kind == "path":
        return str(Path(value).expanduser())
    return value


def _short_path(path: Path | str, *, max_width: int = 80) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        text = "~" + text[len(home) :]
    if len(text) <= max_width:
        return text
    keep = max(12, max_width - 4)
    return "..." + text[-keep:]
