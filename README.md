# Nipux CLI

```text
 _   _ ___ ____  _   ___  __
| \ | |_ _|  _ \| | | \ \/ /
|  \| || || |_) | | | |>  <
| |\  || ||  __/| |_| /_/\_\
|_| \_|___|_|    \__,_|
```

Nipux CLI is a small, restartable local-model worker for long-running browser,
web research, and command-line jobs. It is maintained for Nepox and built around
one practical idea: keep a worker moving in bounded steps, save exact evidence,
learn from each branch, and recover cleanly when a process or model call fails.

- Website: [nepox.com](https://nepox.com)
- Source: [github.com/nipuxx/agent-cli](https://github.com/nipuxx/agent-cli)
- License: [MIT](LICENSE)

## What It Does

Nipux runs jobs that are too long or repetitive for a single chat turn. A job can
search the web, operate a persistent browser profile, write artifacts, inspect
local files with bounded shell commands, update source and finding ledgers, and
continue through a daemon loop until the operator pauses or cancels it.

The default runtime is intentionally narrow:

- one OpenAI-compatible model endpoint
- one SQLite state store under `~/.nipux`
- one restartable daemon with a single-instance lock
- per-job artifact files for exact evidence
- per-job browser profiles through `agent-browser`
- compact memory summaries that point back to artifacts
- visible event history for chat, tools, artifacts, progress, errors, and digests
- durable ledgers for lessons, sources, findings, tasks, and experiments

Nipux does not include a messaging gateway, plugin marketplace, skills manager,
multi-provider setup wizard, RL environment, voice stack, image stack, or broad
web application. The public surface is the `nipux` CLI and the focused
`nipux_cli/` Python package.

## Install

Requirements:

- Python 3.11+
- an OpenAI-compatible chat completions endpoint, local or remote
- optional browser automation: `npm install -g agent-browser && agent-browser install`

Install from a local checkout while developing:

```bash
git clone https://github.com/nipuxx/agent-cli.git
cd agent-cli
uv tool install --editable .
nipux --help
```

Or run without installing:

```bash
uv run nipux --help
```

Install directly from git once the repository is public:

```bash
uv tool install git+https://github.com/nipuxx/agent-cli.git
```

## First Run

Initialize local state under `~/.nipux`. This writes `config.yaml` and a local
`.env` template. Real API keys stay in the environment or `~/.nipux/.env`, not
in the git repo.

```bash
nipux init --openrouter --model openai/gpt-4.1-mini
$EDITOR ~/.nipux/.env
chmod 600 ~/.nipux/.env
nipux doctor --check-model
```

For a local OpenAI-compatible server:

```bash
nipux init --model local-model --base-url http://localhost:8000/v1 --api-key-env OPENAI_API_KEY
nipux doctor
```

Create a job and run a deterministic no-model smoke step:

```bash
nipux create "Research inference optimization ideas and save useful evidence." --title "nightly research"
nipux daemon --once --fake
nipux digest "nightly research"
```

Open the focused chat UI:

```bash
nipux
```

Start the background daemon and inspect progress:

```bash
nipux start
nipux status
nipux activity --follow
```

On macOS, install launchd autostart:

```bash
nipux autostart install --poll-seconds 0
nipux autostart status
```

On Linux, install a user service:

```bash
nipux service install
nipux service status
```

## Secrets

Nipux never needs an API key in `config.yaml`. The config stores only the name
of the environment variable to read:

```yaml
model:
  name: openai/gpt-4.1-mini
  base_url: https://openrouter.ai/api/v1
  api_key_env: OPENROUTER_API_KEY
```

Put secrets in your shell, your process manager, or `~/.nipux/.env`:

```bash
# ~/.nipux/.env
OPENROUTER_API_KEY=
```

The repository includes `.env.example` and `config.example.yaml` as templates.
Do not commit real `.env`, state databases, logs, artifacts, or browser
profiles.

## Local Model Examples

Nipux talks to OpenAI-compatible `/v1/chat/completions` and `/v1/models`
servers. Use any serving stack that supports the model and tool-calling behavior
you want.

SGLang example:

```bash
python -m sglang.launch_server \
  --model-path "$MODEL_NAME" \
  --port 8000 \
  --context-length 262144 \
  --reasoning-parser auto \
  --tool-call-parser auto
```

vLLM example:

```bash
vllm serve "$MODEL_NAME" \
  --port 8000 \
  --max-model-len 262144 \
  --enable-auto-tool-choice \
  --tool-call-parser auto
```

## Operator Workflow

The no-argument CLI opens the focused job directly. Plain text becomes operator
steering for the next worker step, and slash commands inspect or control the
active job.

```text
nipux[nightly research]> what are you working on?
nipux[nightly research]> /history
nipux[nightly research]> /activity
nipux[nightly research]> /outputs
nipux[nightly research]> /artifacts
nipux[nightly research]> /run
nipux[nightly research]> /work 1
nipux[nightly research]> /follow after this branch, compare another source
nipux[nightly research]> /stop
nipux[nightly research]> /shell
nipux[nightly research]> /exit
```

For direct command use:

```bash
uv run nipux status "nightly research" --full
uv run nipux history "nightly research"
uv run nipux events "nightly research" --follow
uv run nipux activity "nightly research" --follow
uv run nipux findings "nightly research"
uv run nipux tasks "nightly research"
uv run nipux experiments "nightly research"
uv run nipux sources "nightly research"
uv run nipux memory "nightly research"
uv run nipux metrics "nightly research"
uv run nipux artifacts "nightly research" --paths
```

Use `nipux health` for daemon truth without opening the dashboard. It reports
the lock state, heartbeat, recent failures, log paths, autostart state, focused
job, and latest daemon events.

## Tool Surface

The worker exposes a deliberately small tool registry:

- `browser_navigate`
- `browser_snapshot`
- `browser_click`
- `browser_type`
- `browser_scroll`
- `browser_back`
- `browser_press`
- `browser_console`
- `web_search`
- `web_extract`
- `shell_exec`
- `write_artifact`
- `read_artifact`
- `search_artifacts`
- `update_job_state`
- `report_update`
- `record_lesson`
- `record_source`
- `record_findings`
- `record_tasks`
- `record_experiment`
- `send_digest_email`

`shell_exec` is bounded with timeouts and output capture. Browser sessions use
per-job profiles under `~/.nipux/browser-profiles/`. Anti-bot, CAPTCHA, login,
and paywall pages are recorded as visible source-quality warnings; Nipux does
not bypass protections.

## Command Reference

```bash
nipux init [--force] [--openrouter] [--model MODEL] [--base-url URL] [--api-key-env ENV]
nipux doctor [--check-model]
nipux shell [--status]
nipux create "objective" [--title TITLE] [--kind KIND] [--cadence CADENCE]
nipux jobs
nipux ls
nipux focus [JOB_TITLE]
nipux rename JOB_TITLE --title NEW_TITLE
nipux delete JOB_TITLE [--keep-files]
nipux chat [JOB_TITLE] [--no-history]
nipux steer [--job JOB_TITLE] MESSAGE
nipux pause [JOB_TITLE] [note...]
nipux resume [JOB_TITLE]
nipux cancel [JOB_TITLE] [note...]
nipux start [--poll-seconds N]
nipux stop
nipux autostart install|status|uninstall [--poll-seconds N]
nipux service install|status|uninstall [--poll-seconds N]
nipux browser-dashboard [--port N] [--foreground] [--stop]
nipux health
nipux status [JOB_TITLE] [--full] [--json]
nipux history [JOB_TITLE] [--full] [--json]
nipux events [JOB_TITLE] [--follow] [--json]
nipux activity [JOB_TITLE] [--follow] [--verbose]
nipux updates [JOB_TITLE]
nipux dashboard [JOB_TITLE]
nipux findings [JOB_TITLE] [--limit N] [--json]
nipux tasks [JOB_TITLE] [--limit N] [--status STATUS] [--json]
nipux experiments [JOB_TITLE] [--limit N] [--status STATUS] [--json]
nipux sources [JOB_TITLE] [--limit N] [--json]
nipux memory [JOB_TITLE]
nipux metrics [JOB_TITLE]
nipux artifacts [JOB_TITLE] [--paths]
nipux artifact QUERY_OR_TITLE [--job JOB_TITLE]
nipux lessons [JOB_TITLE]
nipux learn [--job JOB_TITLE] [--category CATEGORY] LESSON
nipux logs [JOB_TITLE] [--limit N] [--verbose]
nipux outputs [JOB_TITLE] [--limit N] [--verbose]
nipux watch JOB_TITLE [--verbose]
nipux run-one JOB_TITLE [--fake]
nipux work [JOB_TITLE] [--steps N] [--verbose] [--dashboard]
nipux run [JOB_TITLE] [--poll-seconds N] [--no-follow]
nipux daemon [--once] [--fake] [--verbose] [--poll-seconds N]
nipux digest JOB_TITLE
nipux daily-digest [--day YYYY-MM-DD]
```

## Development

```bash
PYTEST_ADDOPTS='' uv run --extra dev python -m pytest -q
uv run --extra dev ruff check --isolated nipux_cli tests/nipux_cli
```

The active implementation notes live in `plans/barebones-24-7-agent.md`.
