from pathlib import Path

from nipux_cli.config import DEFAULT_CONTEXT_LENGTH, load_config


def test_load_config_defaults_to_local_model(tmp_path, monkeypatch):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    config = load_config()

    assert config.runtime.home == tmp_path
    assert config.model.model == "local-model"
    assert config.model.base_url == "http://localhost:8000/v1"
    assert config.model.context_length == DEFAULT_CONTEXT_LENGTH
    assert config.runtime.state_db_path == tmp_path / "state.db"
    assert config.runtime.daily_digest_enabled is True
    assert config.runtime.daily_digest_time == "08:00"


def test_load_config_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
model:
  name: local-test
  base_url: http://127.0.0.1:9999/v1/
  context_length: 12345
runtime:
  home: ./agent-home
  max_step_seconds: 42
  daily_digest_enabled: false
  daily_digest_time: "07:30"
email:
  enabled: true
  to_addr: kai@example.com
""",
        encoding="utf-8",
    )

    config = load_config(cfg)

    assert config.model.model == "local-test"
    assert config.model.base_url == "http://127.0.0.1:9999/v1"
    assert config.model.context_length == 12345
    assert config.runtime.home == Path("./agent-home")
    assert config.runtime.max_step_seconds == 42
    assert config.runtime.daily_digest_enabled is False
    assert config.runtime.daily_digest_time == "07:30"
    assert config.email.enabled is True
    assert config.email.to_addr == "kai@example.com"


def test_load_config_reads_local_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=secret-test-key\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        """
model:
  name: qwen/qwen3.5-27b
  base_url: https://openrouter.ai/api/v1
  api_key_env: OPENROUTER_API_KEY
""",
        encoding="utf-8",
    )

    config = load_config()

    assert config.model.api_key == "secret-test-key"
