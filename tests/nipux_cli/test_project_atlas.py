import importlib.util
import sys
from pathlib import Path


def _load_generator():
    path = Path(__file__).resolve().parents[2] / "scripts" / "generate_project_atlas.py"
    spec = importlib.util.spec_from_file_location("generate_project_atlas", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_project_atlas_generator_maps_prompts_tools_and_source_without_self_embedding():
    generator = _load_generator()

    files = generator.load_source_files()
    prompts = generator.extract_prompts(files)
    tools = generator.extract_tools(files)

    assert "docs/project-atlas.html" not in {source.path for source in files}
    assert any(prompt.path == "nipux_cli/worker.py" and "SYSTEM_PROMPT" in prompt.name for prompt in prompts)
    assert any(tool["name"] == "web_search" for tool in tools)
