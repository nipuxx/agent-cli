# Development Notes

This repo is a focused long-running worker, not a broad assistant distribution.

## Active Surface

- Runtime package: `nipux_cli/`
- Tests: `tests/nipux_cli/`
- Entry point: `nipux`
- State home: `~/.nipux` or `NIPUX_HOME`
- Planning notes: `plans/nipux-runtime-notes.md`

## Constraints

- Keep the default tool surface small and explicit.
- Do not reintroduce broad upstream surfaces such as gateways, skills, plugins, TUI, web UI, ACP, RL environments, voice, image generation, or arbitrary terminal execution.
- Preserve restartability: every worker step should persist state before and after tool execution.
- Store exact evidence as artifacts. Summaries should point back to artifacts instead of replacing them.
- Keep `memory_index` entries compact and artifact-referenced; do not use raw transcript replay as the long-term state strategy.
- Prefer OpenAI-compatible model serving, configured through `~/.nipux/config.yaml`.
- Keep runtime behavior domain-neutral. Do not add task-specific or environment-specific guards, keyword lists, examples, prompts, tools, or tests and describe them as generic framework improvements.

## Validation

Use the focused suite:

```bash
PYTEST_ADDOPTS='' uv run --extra dev python -m pytest -q
uv run --extra dev ruff check --isolated nipux_cli tests/nipux_cli
```

Use `nipux daemon --once --fake` for a no-model smoke test. Use `nipux logs JOB_ID --verbose` or `nipux watch JOB_ID --verbose` when inspecting what a background job is actually doing.
