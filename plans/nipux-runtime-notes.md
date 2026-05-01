# Nipux Runtime Notes

Nipux is a narrow, restartable worker for long-running browser, web research,
and command-line jobs. The active implementation is intentionally small and
centered on `nipux_cli/`, `tests/nipux_cli/`, and the `nipux` console script.

## Runtime Shape

- Package: `nipux_cli/`
- CLI entry point: `nipux`
- State home: `~/.nipux` or `NIPUX_HOME`
- Config file: `~/.nipux/config.yaml`
- Database: SQLite with WAL
- Artifacts: per-job files under the configured state home
- Browser profiles: per-job `agent-browser` profiles
- Model API: OpenAI-compatible chat completions endpoint

## Design Constraints

- Keep every worker step bounded and restartable.
- Persist useful evidence as artifacts before summarizing it.
- Keep summaries compact and point back to artifacts.
- Maintain source, finding, task, experiment, and lesson ledgers.
- Keep jobs runnable until the operator pauses or cancels them.
- Keep the tool registry explicit and small.
- Keep runtime behavior domain-neutral.

## Active Tools

- Browser: `browser_navigate`, `browser_snapshot`, `browser_click`,
  `browser_type`, `browser_scroll`, `browser_back`, `browser_press`,
  `browser_console`
- Web: `web_search`, `web_extract`
- Local command work: `shell_exec`
- Artifacts: `write_artifact`, `read_artifact`, `search_artifacts`
- Job state and visibility: `update_job_state`, `report_update`,
  `send_digest_email`
- Learning ledgers: `record_lesson`, `record_source`, `record_findings`,
  `record_tasks`, `record_experiment`

## Validation

```bash
PYTEST_ADDOPTS='' uv run --extra dev python -m pytest -q
uv run --extra dev ruff check --isolated nipux_cli tests/nipux_cli
uv run nipux doctor
```

Use `uv run nipux daemon --once --fake` for a deterministic no-model smoke
test after CLI or daemon changes.
