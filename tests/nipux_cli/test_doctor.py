import urllib.error

from nipux_cli.config import AppConfig, ModelConfig, RuntimeConfig
from nipux_cli.doctor import run_doctor


def test_doctor_checks_local_runtime_without_model_call(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))

    checks = run_doctor(config=config, check_model=False)

    assert {check.name for check in checks} == {"state_dir_writable", "sqlite", "tool_surface", "browser_runtime"}
    assert all(check.ok for check in checks)


def test_doctor_reports_openrouter_auth_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "bad-key")
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(
            model="qwen/qwen3.6-27b",
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
