import io
import json
import urllib.error

from nipux_cli.config import AppConfig, ModelConfig, RuntimeConfig
from nipux_cli.doctor import run_doctor


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.payload).encode("utf-8")


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


def test_doctor_reports_generation_limit_after_model_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "limited-key")
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(
            model="qwen/qwen3.6-27b",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="TEST_OPENROUTER_KEY",
        ),
    )

    def fake_urlopen(request, timeout):
        url = request.full_url
        if url.endswith("/key"):
            return FakeHTTPResponse({})
        if url.endswith("/models"):
            return FakeHTTPResponse({"data": [{"id": "qwen/qwen3.6-27b"}]})
        if url.endswith("/chat/completions"):
            body = b'{"error":{"message":"Key limit exceeded (total limit).","code":403}}'
            raise urllib.error.HTTPError(url, 403, "Forbidden", hdrs=None, fp=io.BytesIO(body))
        raise AssertionError(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    checks = run_doctor(config=config, check_model=True)
    model_check = checks[-1]

    assert model_check.name == "model_generation"
    assert model_check.ok is False
    assert "Key limit exceeded" in model_check.detail
