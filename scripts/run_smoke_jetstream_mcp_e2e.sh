#!/usr/bin/env bash
# End-to-end: NATS JetStream → worker → MemPalace write, then MCP HTTP search.
# Requires deploy/dev/run_all.sh (or eidolon-memory-mcp) for the HTTP MCP server.
# Uses EIDOLON_MEMORY_SETTINGS_YAML, or local memory.default.yaml if present, else .example template.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

_CFG="${ROOT}/eidolon/memory/config"
if [[ -f "${_CFG}/memory.default.yaml" ]]; then
  export EIDOLON_MEMORY_SETTINGS_YAML="${EIDOLON_MEMORY_SETTINGS_YAML:-${_CFG}/memory.default.yaml}"
else
  export EIDOLON_MEMORY_SETTINGS_YAML="${EIDOLON_MEMORY_SETTINGS_YAML:-${_CFG}/memory.default.yaml.example}"
fi

VENV_PY="${ROOT}/.venv/bin/python"
WORKER="${ROOT}/.venv/bin/eidolon-memory-worker"
MEMPALACE="${ROOT}/.venv/bin/mempalace"

if [[ ! -x "${VENV_PY}" ]]; then
  echo "run: cd ${ROOT} && uv sync" >&2
  exit 1
fi

NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
NATS_PORT="${NATS_PORT:-4222}"
NATS_PID_FILE="/tmp/eidolon-smoke-nats-${NATS_PORT}.pid"
WORKER_PID_FILE="/tmp/eidolon-smoke-worker.pid"

started_nats=0
if ! nc -z 127.0.0.1 "${NATS_PORT}" 2>/dev/null; then
  echo "starting nats-server with JetStream on port ${NATS_PORT}…"
  if ! command -v nats-server >/dev/null 2>&1; then
    echo "nats-server not found; install NATS or set NATS_PORT to your server" >&2
    exit 1
  fi
  mkdir -p "/tmp/eidolon-smoke-nats-js-${NATS_PORT}"
  nats-server -js -p "${NATS_PORT}" -sd "/tmp/eidolon-smoke-nats-js-${NATS_PORT}" >/tmp/eidolon-smoke-nats.log 2>&1 &
  echo $! >"${NATS_PID_FILE}"
  started_nats=1
  sleep 1
else
  echo "NATS already listening on ${NATS_PORT}"
fi

# Bundled defaults use ~/eidolon/mempalace when runtime.palace_path is empty
mkdir -p "${HOME}/eidolon/mempalace"
"${MEMPALACE}" init --yes --no-llm --auto-mine "${HOME}/eidolon/mempalace" 2>/dev/null || true

echo "starting eidolon-memory-worker…"
"${WORKER}" >/tmp/eidolon-smoke-worker.log 2>&1 &
echo $! >"${WORKER_PID_FILE}"
sleep 2

cleanup() {
  if [[ -f "${WORKER_PID_FILE}" ]]; then
    kill "$(cat "${WORKER_PID_FILE}")" 2>/dev/null || true
    rm -f "${WORKER_PID_FILE}"
  fi
  if [[ "${started_nats}" == 1 ]] && [[ -f "${NATS_PID_FILE}" ]]; then
    kill "$(cat "${NATS_PID_FILE}")" 2>/dev/null || true
    rm -f "${NATS_PID_FILE}"
  fi
}
trap cleanup EXIT

echo "running smoke (publish + MCP-style recall)…"
"${VENV_PY}" "${ROOT}/scripts/smoke_jetstream_mcp_e2e.py" "$@"
echo "--- worker log (grep, last lines) ---"
grep -aE "memory_worker|llm_steward|error|stream" /tmp/eidolon-smoke-worker.log 2>/dev/null | tail -n 40 || true
