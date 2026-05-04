"""Configuration for the Nipux long-running agent runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_OPENROUTER_MODEL = "openrouter/auto"
DEFAULT_OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_MODEL = "local-model"
DEFAULT_BASE_URL = "http://localhost:8000/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_CONTEXT_LENGTH = 262_144


def get_agent_home() -> Path:
    """Return the Nipux agent home directory."""

    value = os.environ.get("NIPUX_HOME", "").strip()
    return Path(value).expanduser() if value else Path.home() / ".nipux"


def load_env_file(path: str | Path) -> None:
    """Load KEY=value pairs from a local env file without overriding the shell."""

    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    ensure_private_file_permissions(env_path)
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def ensure_private_file_permissions(path: str | Path) -> None:
    """Best-effort POSIX privacy for local config/secret files."""

    if os.name == "nt":
        return
    try:
        Path(path).chmod(0o600)
    except OSError:
        pass


def ensure_private_dir_permissions(path: str | Path) -> None:
    """Best-effort POSIX privacy for the local Nipux state directory."""

    if os.name == "nt":
        return
    try:
        Path(path).chmod(0o700)
    except OSError:
        pass


def write_private_text(path: str | Path, text: str) -> None:
    """Write text with private file permissions from creation time."""

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
    ensure_private_file_permissions(target)


@dataclass(frozen=True)
class ModelConfig:
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    context_length: int = DEFAULT_CONTEXT_LENGTH
    request_timeout_seconds: float = 120.0
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass(frozen=True)
class RuntimeConfig:
    home: Path = field(default_factory=get_agent_home)
    max_step_seconds: int = 600
    max_steps_per_run: int = 1
    artifact_inline_char_limit: int = 12_000
    daily_digest_enabled: bool = True
    daily_digest_time: str = "08:00"

    @property
    def state_db_path(self) -> Path:
        return self.home / "state.db"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def digests_dir(self) -> Path:
        return self.home / "digests"


@dataclass(frozen=True)
class ToolAccessConfig:
    browser: bool = True
    web: bool = True
    shell: bool = True
    files: bool = True


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password_env: str = "NIPUX_EMAIL_PASSWORD"
    from_addr: str = ""
    to_addr: str = ""
    use_tls: bool = True

    @property
    def password(self) -> str:
        return os.environ.get(self.password_env, "")


@dataclass(frozen=True)
class AppConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    tools: ToolAccessConfig = field(default_factory=ToolAccessConfig)
    email: EmailConfig = field(default_factory=EmailConfig)

    def ensure_dirs(self) -> None:
        for directory in (
            self.runtime.home,
            self.runtime.jobs_dir,
            self.runtime.logs_dir,
            self.runtime.digests_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            ensure_private_dir_permissions(directory)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml, falling back to a local OpenAI-compatible endpoint."""

    home = get_agent_home()
    load_env_file(home / ".env")
    cfg_path = Path(path).expanduser() if path else home / "config.yaml"
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        raw = _as_dict(loaded)

    runtime_raw = _as_dict(raw.get("runtime"))
    model_raw = _as_dict(raw.get("model"))
    tools_raw = _as_dict(raw.get("tools"))
    email_raw = _as_dict(raw.get("email"))

    runtime_home = Path(runtime_raw.get("home") or home).expanduser()
    runtime = RuntimeConfig(
        home=runtime_home,
        max_step_seconds=int(runtime_raw.get("max_step_seconds", 600)),
        max_steps_per_run=int(runtime_raw.get("max_steps_per_run", 1)),
        artifact_inline_char_limit=int(runtime_raw.get("artifact_inline_char_limit", 12_000)),
        daily_digest_enabled=bool(runtime_raw.get("daily_digest_enabled", True)),
        daily_digest_time=str(runtime_raw.get("daily_digest_time") or "08:00"),
    )
    model = ModelConfig(
        model=str(model_raw.get("name") or model_raw.get("model") or DEFAULT_MODEL),
        base_url=str(model_raw.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        api_key_env=str(model_raw.get("api_key_env") or DEFAULT_API_KEY_ENV),
        context_length=int(model_raw.get("context_length", DEFAULT_CONTEXT_LENGTH)),
        request_timeout_seconds=float(model_raw.get("request_timeout_seconds", 120.0)),
        input_cost_per_million=_optional_float(model_raw.get("input_cost_per_million")),
        output_cost_per_million=_optional_float(model_raw.get("output_cost_per_million")),
    )
    tools = ToolAccessConfig(
        browser=bool(tools_raw.get("browser", True)),
        web=bool(tools_raw.get("web", True)),
        shell=bool(tools_raw.get("shell", True)),
        files=bool(tools_raw.get("files", True)),
    )
    email = EmailConfig(
        enabled=bool(email_raw.get("enabled", False)),
        smtp_host=str(email_raw.get("smtp_host") or ""),
        smtp_port=int(email_raw.get("smtp_port", 587)),
        username=str(email_raw.get("username") or ""),
        password_env=str(email_raw.get("password_env") or "NIPUX_EMAIL_PASSWORD"),
        from_addr=str(email_raw.get("from_addr") or ""),
        to_addr=str(email_raw.get("to_addr") or ""),
        use_tls=bool(email_raw.get("use_tls", True)),
    )
    return AppConfig(runtime=runtime, model=model, tools=tools, email=email)


def default_config_yaml(
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    context_length: int = DEFAULT_CONTEXT_LENGTH,
) -> str:
    """Return a starter config file for an OpenAI-compatible model server."""

    return (
        "model:\n"
        f"  name: {model}\n"
        f"  base_url: {base_url.rstrip('/')}\n"
        f"  api_key_env: {api_key_env}\n"
        f"  context_length: {context_length}\n"
        "  input_cost_per_million: null\n"
        "  output_cost_per_million: null\n"
        "runtime:\n"
        "  max_step_seconds: 600\n"
        "  max_steps_per_run: 1\n"
        "  artifact_inline_char_limit: 12000\n"
        "  daily_digest_enabled: true\n"
        "  daily_digest_time: \"08:00\"\n"
        "tools:\n"
        "  browser: true\n"
        "  web: true\n"
        "  shell: true\n"
        "  files: true\n"
        "email:\n"
        "  enabled: false\n"
        "  smtp_host: \"\"\n"
        "  smtp_port: 587\n"
        "  username: \"\"\n"
        "  password_env: NIPUX_EMAIL_PASSWORD\n"
        "  from_addr: \"\"\n"
        "  to_addr: \"\"\n"
    )
