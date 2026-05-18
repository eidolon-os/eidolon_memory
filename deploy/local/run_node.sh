#!/usr/bin/env bash
# Start local memory node: worker + MCP HTTP with CPU guards for LiveKit co-hosting.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"

echo "[INFO] CPU threads: auto via cpu_env (override with OMP_NUM_THREADS in env)"

if [[ "${1:-}" == "stop" ]]; then
  exec "${REPO_ROOT}/deploy/dev/run_all.sh" stop
fi

if [[ "${1:-}" == "status" ]]; then
  exec "${REPO_ROOT}/deploy/dev/run_all.sh" status
fi

"${REPO_ROOT}/deploy/dev/run_all.sh" start
