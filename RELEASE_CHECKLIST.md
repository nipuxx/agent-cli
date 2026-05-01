# Release Checklist

Use this before sharing the repository with outside users.

## Secrets

- No real API keys in git.
- `config.yaml` examples use `model.api_key_env`, not literal keys.
- `.env`, `.env.*`, state databases, logs, artifacts, and browser profiles are ignored.
- `nipux doctor` reports missing remote API-key environment variables without printing key values.

## Install

- `uv tool install --editable .` works from a checkout.
- `uv run nipux --help` works without installing.
- `NIPUX_HOME=$(mktemp -d) uv run nipux` opens the first-run terminal UI, not argparse help or an ASCII-only prompt.
- `nipux init` writes the default Qwen/OpenRouter `~/.nipux/config.yaml` and a blank `~/.nipux/.env` template.
- `nipux doctor` passes for local runtime checks after initialization.
- `nipux daemon --once --fake` runs without a model key.

## Runtime

- `nipux start`, `nipux stop`, and `nipux restart` recover stale daemon state.
- `nipux status`, `nipux activity`, `nipux history`, and `nipux artifacts` expose enough state to debug jobs.
- Worker prompts stay bounded and do not replay raw transcript history.
- Operator chat that is only conversational stays in history but does not remain active worker context.
- Measurable jobs record experiments instead of treating notes as progress.
- Status, outcomes, and work panes show different layers clearly: jobs and latest outputs, durable progress by hour, and raw tool/console events.

## Validation

```bash
python -m compileall nipux_cli tests/nipux_cli
uv run --extra dev python -m pytest tests/nipux_cli -q
uv run --extra dev ruff check nipux_cli tests/nipux_cli
rg -n --hidden -S "(sk-[A-Za-z0-9_-]{20,}|OPENROUTER_API_KEY[=].+|OPENAI_API_KEY[=].+|Bearer\\s+[A-Za-z0-9._-]{20,})" . \
  -g '!uv.lock' -g '!**/__pycache__/**' -g '!*.db' -g '!*.log' -g '!*.pyc'
```
