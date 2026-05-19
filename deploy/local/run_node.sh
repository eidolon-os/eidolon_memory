#!/usr/bin/env bash
# Local memory node = supervisor (D1): start / stop / reload / status / restart.
# Thin alias over deploy/dev/run_all.sh so local installs can keep their muscle memory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "[INFO] CPU threads: auto via cpu_env (override with OMP_NUM_THREADS in env)"
exec "${REPO_ROOT}/deploy/dev/run_all.sh" "${1:-start}" "${@:2}"
