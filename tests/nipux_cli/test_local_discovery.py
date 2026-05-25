from pathlib import Path

import nipux_cli.first_run_frame_runtime as first_run_runtime
import nipux_cli.local_discovery as local_discovery
from nipux_cli.local_discovery import (
    CommandResult,
    LocalModelConfig,
    choose_recommended_local_model,
    discover_local_environment,
)


def _runner(results):
    def run(argv, timeout=1.0):
        del timeout
        return results.get(tuple(argv), CommandResult(127, "", f"{argv[0]} not found"))

    return run


def test_discovery_recommends_running_ollama_model(monkeypatch, tmp_path):
    monkeypatch.setattr(local_discovery.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "ollama" else None)
    monkeypatch.setattr(local_discovery.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_discovery.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local_discovery.platform, "processor", lambda: "generic cpu")
    runner = _runner(
        {
            ("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"): CommandResult(
                0, "RTX 4090, 24564 MiB\n", ""
            ),
            ("ollama", "list"): CommandResult(
                0,
                "NAME                 ID              SIZE      MODIFIED\n"
                "qwen3.6:27b-q4       abc123          15 GB     now\n",
                "",
            ),
        }
    )

    report = discover_local_environment(runner=runner, home=tmp_path)

    assert report.hardware.accelerators == ("NVIDIA RTX 4090, 24564 MiB",)
    assert report.recommended == LocalModelConfig(
        "Ollama",
        "qwen3.6:27b-q4",
        "http://localhost:11434/v1",
        "running local endpoint with installed model",
    )
    assert "qwen3.6:27b-q4" in report.runtimes[0].models


def test_discovery_lists_suggested_models_when_no_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(local_discovery.shutil, "which", lambda _name: None)

    report = discover_local_environment(runner=_runner({}), home=tmp_path)

    assert report.recommended is None
    assert report.installed_runtimes == ()
    assert any("qwen3.6:27b" in suggestion for suggestion in report.suggestions)
    assert any("qwen3.6:35b-a3b" in suggestion for suggestion in report.suggestions)


def test_llama_cpp_detector_finds_local_gguf(monkeypatch, tmp_path):
    monkeypatch.setattr(local_discovery.shutil, "which", lambda name: "/usr/bin/llama-server" if name == "llama-server" else None)
    model_path = tmp_path / "Models" / "Qwen3.6-27B-Q4_K_M.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_text("fake", encoding="utf-8")

    runtime = local_discovery.detect_llama_cpp(home=tmp_path)

    assert runtime.installed is True
    assert runtime.base_url == "http://localhost:8080/v1"
    assert "Qwen3.6-27B-Q4_K_M" in runtime.models


def test_recommendation_prefers_running_endpoint_with_models():
    recommended = choose_recommended_local_model(
        [
            local_discovery.RuntimeInfo("Offline", True, False, base_url="http://localhost:1/v1", models=("offline",)),
            local_discovery.RuntimeInfo("Running", True, True, base_url="http://localhost:2/v1", models=("live",)),
        ]
    )

    assert recommended == LocalModelConfig("Running", "live", "http://localhost:2/v1", "running local endpoint with installed model")


def test_first_run_prefills_discovered_local_config(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    monkeypatch.setattr(
        first_run_runtime,
        "recommended_local_model_config",
        lambda: LocalModelConfig("Ollama", "qwen3.6:27b-q4", "http://localhost:11434/v1", "test"),
    )

    assert first_run_runtime.first_run_edit_initial_value("model.base_url") == "http://localhost:11434/v1"
    assert first_run_runtime.first_run_edit_initial_value("model.name") == "qwen3.6:27b-q4"
    assert first_run_runtime.first_run_edit_initial_value("secret:model.api_key") == "skip"


def test_first_run_cycles_discovered_models_and_endpoints(monkeypatch):
    report = local_discovery.LocalDiscoveryReport(
        hardware=local_discovery.HardwareInfo("Linux", "x86_64", "cpu"),
        runtimes=(
            local_discovery.RuntimeInfo(
                "Ollama",
                True,
                True,
                base_url="http://localhost:11434/v1",
                models=("qwen3.6:27b-q4", "qwen3.6:35b-a3b-q4"),
            ),
            local_discovery.RuntimeInfo(
                "LM Studio",
                True,
                False,
                base_url="http://localhost:1234/v1",
                models=("local/lmstudio-model",),
            ),
        ),
        recommended=LocalModelConfig("Ollama", "qwen3.6:27b-q4", "http://localhost:11434/v1", "test"),
    )
    monkeypatch.setattr(first_run_runtime, "cached_local_discovery", lambda: report)

    assert (
        first_run_runtime.first_run_cycle_discovered_value("model.name", "qwen3.6:27b-q4", direction=1)
        == "qwen3.6:35b-a3b-q4"
    )
    assert (
        first_run_runtime.first_run_cycle_discovered_value("model.base_url", "http://localhost:11434/v1", direction=1)
        == "http://localhost:1234/v1"
    )
