import io
import json
import urllib.error

from nipux_cli.config import AppConfig, ModelConfig, RuntimeConfig
from nipux_cli.doctor import Check, doctor_check_status, doctor_checks_ready, run_doctor


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


def test_doctor_reports_remote_generation_auth_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "bad-key")
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(
            model="provider/model",
            base_url="https://provider.example/v1",
            api_key_env="TEST_PROVIDER_KEY",
        ),
    )

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    checks = run_doctor(config=config, check_model=True)
    model_check = checks[-1]

    assert model_check.name == "model_generation"
    assert model_check.ok is False
    assert "401" in model_check.detail


def test_doctor_reports_generation_limit_after_model_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "limited-key")
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(
            model="provider/test-model",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="TEST_OPENROUTER_KEY",
        ),
    )

    def fake_urlopen(request, timeout):
        url = request.full_url
        if url.endswith("/key"):
            return FakeHTTPResponse({})
        if url.endswith("/models"):
            return FakeHTTPResponse({"data": [{"id": "provider/test-model"}]})
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


def test_doctor_reports_error_body_when_provider_returns_no_choices(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "working-key")
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(
            model="provider/test-model",
            base_url="https://provider.example/v1",
            api_key_env="TEST_PROVIDER_KEY",
        ),
    )

    def fake_urlopen(request, timeout):
        url = request.full_url
        if url.endswith("/chat/completions"):
            return FakeHTTPResponse({"error": {"message": "tool schema rejected by provider", "code": 400}})
        raise AssertionError(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    checks = run_doctor(config=config, check_model=True)
    model_check = checks[-1]

    assert model_check.name == "model_generation"
    assert model_check.ok is False
    assert "tool schema rejected by provider" in model_check.detail


def test_browser_runtime_failure_does_not_block_model_setup():
    checks = [
        Check("state_dir_writable", True, "ok"),
        Check("sqlite", True, "ok"),
        Check("model_config", True, "ok"),
        Check("tool_surface", True, "ok"),
        Check("browser_runtime", False, "missing"),
        Check("model_endpoint", True, "chat accepted"),
    ]

    assert doctor_checks_ready(checks, check_model=True) is True
    assert doctor_check_status(checks[4]) == "warn"


def test_doctor_reports_nested_provider_generation_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "limited-key")
    config = AppConfig(
        runtime=RuntimeConfig(home=tmp_path),
        model=ModelConfig(
            model="provider/test-model",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="TEST_OPENROUTER_KEY",
        ),
    )

    def fake_urlopen(request, timeout):
        url = request.full_url
        if url.endswith("/key"):
            return FakeHTTPResponse({})
        if url.endswith("/models"):
            return FakeHTTPResponse({"data": [{"id": "provider/test-model"}]})
        if url.endswith("/chat/completions"):
            body = json.dumps(
                {
                    "error": {
                        "message": "Provider returned error",
                        "code": 429,
                        "metadata": {
                            "raw": "provider/test-model is temporarily rate-limited upstream.",
                            "provider_name": "ExampleProvider",
                            "is_byok": False,
                        },
                    }
                }
            ).encode("utf-8")
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(body))
        raise AssertionError(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    checks = run_doctor(config=config, check_model=True)
    model_check = checks[-1]

    assert model_check.name == "model_generation"
    assert model_check.ok is False
    assert "Provider returned error" in model_check.detail
    assert "temporarily rate-limited upstream" in model_check.detail
    assert "provider=ExampleProvider" in model_check.detail
    assert "byok=False" in model_check.detail
