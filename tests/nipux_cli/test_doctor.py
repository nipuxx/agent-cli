import urllib.error

from nipux_cli.config import AppConfig, ModelConfig, RuntimeConfig
from nipux_cli.doctor import run_doctor


def test_doctor_checks_local_runtime_without_model_call(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))

    checks = run_doctor(config=config, check_model=False)

    assert {check.name for check in checks} == {"state_dir_writable", "sqlite", "model_config", "tool_surface", "browser_runtime"}
    assert all(check.ok for check in checks)


def test_doctor_warns_when_remote_model_key_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(
            model="provider/model",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="OPENROUTER_API_KEY",
        ),
    )

    checks = run_doctor(config=config, check_model=False)
    model_check = next(check for check in checks if check.name == "model_config")

    assert not model_check.ok
    assert "OPENROUTER_API_KEY is not set" in model_check.detail
    assert "sk-" not in model_check.detail


def test_doctor_reports_openrouter_auth_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "bad-key")
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(
            model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="TEST_OPENROUTER_KEY",
        ),
    )

    def fake_urlopen(_request, timeout):
        raise urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/key",
            401,
            "Unauthorized",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    checks = run_doctor(config=config, check_model=True)
    model_check = checks[-1]

    assert model_check.name == "model_auth"
    assert model_check.ok is False
    assert "OpenRouter rejected API key" in model_check.detail
