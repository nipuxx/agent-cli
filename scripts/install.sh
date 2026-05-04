#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${NIPUX_REPO_URL:-https://github.com/nipuxx/agent-cli.git}"
REF="${NIPUX_REF:-main}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found; installing uv first"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv install did not put uv on PATH. Restart your shell and rerun this installer." >&2
  exit 1
fi

uv tool install --upgrade "git+${REPO_URL}@${REF}"

echo
echo "Nipux installed. Opening first-run setup."
exec nipux
