from types import SimpleNamespace

from nipux_cli.cli import _ensure_remote_model_ready_for_worker
from nipux_cli.doctor import Check


def _config(base_url: str):
    return SimpleNamespace(model=SimpleNamespace(base_url=base_url))


def test_remote_model_preflight_blocks_rejected_auth(monkeypatch, capsys):
    def fake_doctor(*, config, check_model):
        assert check_model is True
        return [Check("model_auth", False, "OpenRouter rejected API key: User not found")]

    monkeypatch.setattr("nipux_cli.cli.run_doctor", fake_doctor)

    assert _ensure_remote_model_ready_for_worker(_config("https://openrouter.ai/api/v1"), fake=False) is False

    out = capsys.readouterr().out
    assert "model is not ready; daemon not started" in out
    assert "model_auth: OpenRouter rejected API key" in out
    assert "doctor --check-model" in out


def test_remote_model_preflight_skips_fake_runs(monkeypatch):
    def fake_doctor(*, config, check_model):
        raise AssertionError("fake runs should not need remote model auth")

    monkeypatch.setattr("nipux_cli.cli.run_doctor", fake_doctor)

    assert _ensure_remote_model_ready_for_worker(_config("https://openrouter.ai/api/v1"), fake=True) is True


def test_remote_model_preflight_does_not_block_local_endpoints(monkeypatch):
    def fake_doctor(*, config, check_model):
        raise AssertionError("local endpoints are checked by the worker, not daemon preflight")

    monkeypatch.setattr("nipux_cli.cli.run_doctor", fake_doctor)

    assert _ensure_remote_model_ready_for_worker(_config("http://localhost:11434/v1"), fake=False) is True

