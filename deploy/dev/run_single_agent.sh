#!/usr/bin/env bash
# Run ONE agent_runner in the foreground for debugging (D1).
#
# This is the dev companion to run_all.sh (which manages the supervisor + many
# agents). Use it when you want to:
#   * stare at log output for one user
#   * attach a debugger
#   * reproduce a crash without disturbing the supervised set
#
# Examples:
#   ./deploy/dev/run_single_agent.sh --user-id alice
#   ./deploy/dev/run_single_agent.sh --user-id bench --port 18030
#   ./deploy/dev/run_single_agent.sh --user-id alice --palace-path /tmp/probe
#
# Pre-reqs:
#   uv sync --extra dev   # once
#   ./deploy/dev/init.sh --user-id alice   # init that user's palace
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
[[ -f "$REPO_ROOT/pyproject.toml" ]] || {
  echo "[ERROR] cannot resolve repo root from $0" >&2
  exit 1
}
cd "$REPO_ROOT"

unset VIRTUAL_ENV

if ! command -v uv >/dev/null 2>&1; then
  echo "[ERROR] uv not on PATH (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

# Pass through all args; agent_runner has its own argparse (--user-id required).
# Foreground = stdout/stderr stay on the TTY; Ctrl-C cleans up.
exec uv run eidolon-memory-agent "$@"
