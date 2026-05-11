import importlib.util
import sys
from pathlib import Path

from nipux_cli.memory_graph import memory_graph_for_prompt


def _load_live_smoke():
    path = Path(__file__).resolve().parents[2] / "scripts" / "live_memory_graph_smoke.py"
    spec = importlib.util.spec_from_file_location("live_memory_graph_smoke", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_memory_graph_smoke_fails_cleanly_without_key(monkeypatch, capsys):
    smoke = _load_live_smoke()
    monkeypatch.delenv("NIPUX_LIVE_TEST_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["live_memory_graph_smoke.py", "--api-key-env", "NIPUX_LIVE_TEST_KEY", "--json"])

    assert smoke.main() == 1

    out = capsys.readouterr().out
    assert '"success": false' in out
    assert "NIPUX_LIVE_TEST_KEY is not set" in out
    assert "secret" not in out.lower()


def test_live_memory_graph_smoke_seed_pushes_generic_consolidation():
    smoke = _load_live_smoke()
    prompt = memory_graph_for_prompt({"metadata": smoke._seed_metadata()})

    assert "No memory graph yet" in prompt
    assert "Durable ledgers already contain" in prompt
