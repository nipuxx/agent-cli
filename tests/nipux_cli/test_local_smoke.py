import importlib.util
import tempfile
from pathlib import Path


def _load_local_smoke():
    path = Path(__file__).resolve().parents[2] / "scripts" / "local_smoke.py"
    spec = importlib.util.spec_from_file_location("local_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_smoke_defaults_to_temporary_profile():
    module = _load_local_smoke()

    assert str(module.DEFAULT_HOME).startswith(tempfile.gettempdir())
    assert module._safe_reset_path(module.DEFAULT_HOME)


def test_local_smoke_refuses_to_reset_real_profile():
    module = _load_local_smoke()

    assert not module._safe_reset_path(Path.home() / ".nipux")


def test_local_smoke_command_text_uses_isolated_profile():
    module = _load_local_smoke()

    text = module._command_text(["status", "local smoke"], module.DEFAULT_HOME)

    assert "NIPUX_HOME=" in text
    assert "NIPUX_PLAIN=1" in text
    assert "python -m nipux_cli status 'local smoke'" in text
