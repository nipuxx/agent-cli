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
    assert any(prompt.path == "nipux_cli/worker_policy.py" and "SYSTEM_PROMPT" in prompt.name for prompt in prompts)
    assert any(tool["name"] == "web_search" for tool in tools)


def test_project_atlas_redacts_secret_assignments_from_rendered_source():
    generator = _load_generator()
    openrouter_key = "OPENROUTER_API_KEY"
    openai_key = "OPENAI_API_KEY"
    source = generator.SourceFile(
        path=".env.example",
        text=f"{openrouter_key}=\n{openai_key}=secret\nNORMAL=value",
        lines=[openrouter_key + "=", openai_key + "=secret", "NORMAL=value"],
        tree=None,
    )

    rendered = generator.render_source_file(source)

    assert openrouter_key + "=" not in rendered
    assert openai_key + "=secret" not in rendered
    assert f"{openrouter_key} = &lt;redacted&gt;" in rendered
    assert f"{openai_key} = &lt;redacted&gt;" in rendered
    assert "NORMAL=value" in rendered
