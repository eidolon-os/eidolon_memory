#!/usr/bin/env bash
# Orchestrate D1 memory benchmarks: pytest + R-01 (recall) + W-01 (write→visible).
#
#   ./scripts/benchmark/run_memory_perf_report.sh
#   ./scripts/benchmark/run_memory_perf_report.sh --memory-space-id default.bench.mochi --port 18030 --read-count 100
#
# Brings up a single agent_runner subprocess for ``--memory-space-id``, runs the read
# load generator + the publish→recall-visible bench, then tears the agent down.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
export no_proxy="${no_proxy:-127.0.0.1,localhost}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
AGENT_BIN="${AGENT_BIN:-${REPO_ROOT}/.venv/bin/eidolon-memory-agent}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "[ERROR] python executable not found: ${PYTHON_BIN}" >&2
  exit 1
fi
if [[ ! -x "${AGENT_BIN}" ]]; then
  echo "[ERROR] eidolon-memory-agent executable not found: ${AGENT_BIN}" >&2
  exit 1
fi

MEMORY_SPACE_ID="default.bench.mochi"
TENANT_ID="default"
OWNER_USER_ID="bench"
PERSONA_ID="mochi"
AGENT_ID="agent-bench"
DEVICE_ID="bench-device"
INSTANCE_ID="bench-runtime"
PORT="18030"
READ_COUNT="50"
WRITE_COUNT="5"
RUN_PYTEST="1"
RUN_WRITE="1"
W01_STATUS="skipped"
SEED_SIZE=""   # "" = skip seed; S/M/L = seed N drawers
VOICE_FLAG=""

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "Options:"
  echo "  --memory-space-id <id> (default: default.bench.mochi)"
  echo "  --tenant-id <id>       (default: default)"
  echo "  --owner-user-id <id>   (default: bench)"
  echo "  --persona-id <id>      (default: mochi)"
  echo "  --agent-id <id>        (default: agent-bench)"
  echo "  --device-id <id>       (default: bench-device)"
  echo "  --port <port>      (default: 18030)"
  echo "  --read-count <n>   R-01 sample count (default: 50)"
  echo "  --write-count <n>  W-01 sample count (default: 5)"
  echo "  --skip-pytest      do not run pytest"
  echo "  --skip-write       do not run W-01 (publish→visible)"
  echo "  --voice            run R-01 with LiveKit shared-embedding hot path"
  echo "  --seed S|M|L       seed palace with 100/1000/5000 drawers before R-01"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --memory-space-id) MEMORY_SPACE_ID="$2"; shift 2 ;;
    --tenant-id) TENANT_ID="$2"; shift 2 ;;
    --owner-user-id) OWNER_USER_ID="$2"; shift 2 ;;
    --persona-id) PERSONA_ID="$2"; shift 2 ;;
    --agent-id) AGENT_ID="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --read-count) READ_COUNT="$2"; shift 2 ;;
    --write-count) WRITE_COUNT="$2"; shift 2 ;;
    --skip-pytest) RUN_PYTEST=0; shift ;;
    --skip-write) RUN_WRITE=0; shift ;;
    --voice) VOICE_FLAG="--voice"; shift ;;
    --seed) SEED_SIZE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[ERROR] unknown arg: $1" >&2; usage >&2; exit 1 ;;
  esac
done

IFS='.' read -r TENANT_ID OWNER_USER_ID PERSONA_ID <<< "${MEMORY_SPACE_ID}"
if [[ -z "${TENANT_ID}" || -z "${OWNER_USER_ID}" || -z "${PERSONA_ID}" ]]; then
  echo "[ERROR] --memory-space-id must be <tenant>.<owner_user>.<persona>" >&2
  exit 1
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${REPO_ROOT}/reports/memory_perf_${STAMP}"
mkdir -p "$OUT_DIR"
echo "[INFO] output: $OUT_DIR"

BENCH_HOME="${OUT_DIR}/home"
mkdir -p "${BENCH_HOME}/.cache"
export HOME="${BENCH_HOME}"
export XDG_CACHE_HOME="${BENCH_HOME}/.cache"
export MEMPALACE_BACKEND="sqlite_exact"

BENCH_SETTINGS="${OUT_DIR}/memory_settings.yaml"
cat > "${BENCH_SETTINGS}" <<YAML
steward:
  mode: rules
mempalace:
  backend: sqlite_exact
mcp_http:
  host: 127.0.0.1
  port: ${PORT}
runtime:
  palaces_root: ${OUT_DIR}/palaces
nats:
  url: nats://127.0.0.1:4222
YAML
export EIDOLON_MEMORY_SETTINGS_YAML="${BENCH_SETTINGS}"

GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo n/a)"
GIT_STATUS="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

# ----- pytest -----
PYTEST_LOG="${OUT_DIR}/pytest.log"
PYTEST_STATUS="skipped"
if [[ "$RUN_PYTEST" -eq 1 ]]; then
  echo "[INFO] pytest migrated multi-device benchmark gates"
  if "${PYTHON_BIN}" -m pytest \
    tests/memory/test_palace_directory.py \
    tests/memory/test_multidevice_memory.py \
    tests/memory/e2e/test_multidevice_architecture.py \
    -q --tb=short 2>&1 | tee "$PYTEST_LOG"; then
    PYTEST_STATUS="PASS"
  else
    PYTEST_STATUS="FAIL"
  fi
fi

if [[ "$RUN_WRITE" -eq 1 ]]; then
  if ! "${PYTHON_BIN}" - "${MEMORY_SPACE_ID}" <<'PY'
import asyncio
import sys

import nats
from eidolon_sdk.memory import conversation_turn_subject, memory_command_subject, memory_sync_subject

from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.infrastructure.nats.names import memory_consumer_name
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream


async def main() -> None:
    settings = get_memory_settings()
    memory_space_id = sys.argv[1]
    created_durables = []
    nc = await nats.connect(
        settings.nats.url,
        connect_timeout=1,
        max_reconnect_attempts=0,
    )
    try:
        js = nc.jetstream()
        await asyncio.wait_for(js.account_info(), timeout=1.0)
        await asyncio.wait_for(ensure_memory_stream(js, settings), timeout=2.0)
        checks = [
            (conversation_turn_subject(memory_space_id), "preflight"),
            (memory_command_subject(memory_space_id), "preflight_cmd"),
            (memory_sync_subject(memory_space_id), "preflight_sync"),
        ]
        for subject, role in checks:
            durable = memory_consumer_name(settings.nats.durable_prefix, memory_space_id, role=role)
            await asyncio.wait_for(
                js.pull_subscribe(subject, durable=durable, stream=settings.nats.stream),
                timeout=2.0,
            )
            created_durables.append(durable)
    finally:
        js = nc.jetstream()
        for durable in created_durables:
            try:
                await asyncio.wait_for(
                    js.delete_consumer(settings.nats.stream, durable),
                    timeout=1.0,
                )
            except Exception:
                pass
        await nc.drain()


try:
    asyncio.run(main())
except Exception:
    raise SystemExit(2)
PY
  then
    echo "[WARN] local NATS unavailable on nats://127.0.0.1:4222; skipping W-01 and starting read-only agent"
    RUN_WRITE=0
    W01_STATUS="skipped-nats-unavailable"
  fi
fi

if [[ "$RUN_WRITE" -eq 0 ]]; then
  export EIDOLON_MEMORY_DISABLE_NATS=1
fi

# ----- seed palace (optional) -----
if [[ -n "$SEED_SIZE" ]]; then
  # D1: resolve palace path via the same helper agent_runner uses; never hardcode.
  PALACE_DIR=$("${PYTHON_BIN}" -c "
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_for_memory_space
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized
from eidolon.memory.infrastructure.mempalace_backend import mempalace_backend_env, selected_mempalace_backend
import sys
settings = get_memory_settings()
palace = resolve_palace_for_memory_space(settings, sys.argv[1])
backend = selected_mempalace_backend(settings)
ensure_palace_initialized(sys.argv[1], palace, backend=backend, env=mempalace_backend_env(settings))
print(palace)
" "$MEMORY_SPACE_ID")
  echo "[INFO] seeding palace ${PALACE_DIR} size=${SEED_SIZE}"
  "${PYTHON_BIN}" scripts/benchmark/seed_palace.py \
    --palace "${PALACE_DIR}" --size "${SEED_SIZE}" \
    --memory-space-id "${MEMORY_SPACE_ID}" --device-id "${DEVICE_ID}" \
    2>&1 | tee "${OUT_DIR}/seed.log"
fi

# ----- bring up agent_runner -----
AGENT_LOG="${OUT_DIR}/agent_runner.log"
echo "[INFO] starting eidolon-memory-agent --memory-space-id=${MEMORY_SPACE_ID} --port=${PORT}"
"${AGENT_BIN}" --memory-space-id="${MEMORY_SPACE_ID}" --port="${PORT}" \
  >"${AGENT_LOG}" 2>&1 &
AGENT_PID=$!
echo "[INFO] agent_runner PID=${AGENT_PID}, log=${AGENT_LOG}"

cleanup() {
  if kill -0 "${AGENT_PID}" 2>/dev/null; then
    echo "[INFO] stopping agent_runner PID=${AGENT_PID}"
    kill "${AGENT_PID}" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      sleep 1
      kill -0 "${AGENT_PID}" 2>/dev/null || break
    done
    kill -9 "${AGENT_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Wait for control plane to bind
for i in $(seq 1 30); do
  if nc -z 127.0.0.1 "${PORT}" 2>/dev/null; then
    echo "[INFO] control-plane up after ${i}s"
    break
  fi
  sleep 1
done
nc -z 127.0.0.1 "${PORT}" 2>/dev/null || {
  echo "[ERROR] agent_runner did not bind ${PORT} within 30s" >&2
  tail -40 "${AGENT_LOG}" >&2 || true
  exit 1
}

# Give warm (ONNX + closets + voice wing dry-run) a chance to finish.
# `agent_runner_warm_complete` shows once warm path is fully primed.
for i in $(seq 1 30); do
  if grep -q "agent_runner_warm_complete" "${AGENT_LOG}" 2>/dev/null; then
    echo "[INFO] warm complete after ~${i}s"
    break
  fi
  sleep 1
done

# ----- R-01 -----
R01_JSON="${OUT_DIR}/R-01.json"
R01_LOG="${OUT_DIR}/R-01.log"
echo "[INFO] R-01 read bench (n=${READ_COUNT}) ${VOICE_FLAG}"
if "${PYTHON_BIN}" scripts/benchmark/bench_read_livekit.py \
  --url "http://127.0.0.1:${PORT}/mcp" \
  --tenant-id "${TENANT_ID}" \
  --owner-user-id "${OWNER_USER_ID}" \
  --persona-id "${PERSONA_ID}" \
  --agent-id "${AGENT_ID}" \
  --device-id "${DEVICE_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --count "${READ_COUNT}" ${VOICE_FLAG} \
  --out "${R01_JSON}" 2>&1 | tee "${R01_LOG}"; then
  R01_STATUS="PASS"
else
  R01_STATUS="FAIL"
fi

# ----- W-01 -----
W01_JSON="${OUT_DIR}/W-01.json"
W01_LOG="${OUT_DIR}/W-01.log"
if [[ "$RUN_WRITE" -eq 1 ]]; then
  echo "[INFO] W-01 write→visible bench (n=${WRITE_COUNT})"
  if "${PYTHON_BIN}" scripts/benchmark/bench_write_jetstream.py \
    --count "${WRITE_COUNT}" \
    --tenant-id "${TENANT_ID}" \
    --owner-user-id "${OWNER_USER_ID}" \
    --persona-id "${PERSONA_ID}" \
    --agent-id "${AGENT_ID}" \
    --device-id "${DEVICE_ID}" \
    --instance-id "${INSTANCE_ID}" \
    --mcp-url "http://127.0.0.1:${PORT}/mcp" \
    --out "${W01_JSON}" 2>&1 | tee "${W01_LOG}"; then
    W01_STATUS="PASS"
  else
    W01_STATUS="FAIL"
  fi
fi

cleanup
trap - EXIT INT TERM

# ----- assemble summary.md -----
SUMMARY="${OUT_DIR}/summary.md"
{
  echo "# Eidolon Memory D1 perf report"
  echo ""
  echo "- timestamp: ${STAMP}"
  echo "- git: ${GIT_SHA} (dirty=${GIT_STATUS})"
  echo "- memory_space_id: ${MEMORY_SPACE_ID}, port: ${PORT}"
  echo "- OMP_NUM_THREADS=${OMP_NUM_THREADS} MKL_NUM_THREADS=${MKL_NUM_THREADS}"
  echo "- settings: memory_settings.yaml (steward.mode=rules)"
  echo ""
  echo "| 阶段 | 状态 | log |"
  echo "|------|------|------|"
  echo "| pytest | ${PYTEST_STATUS} | [pytest.log](pytest.log) |"
  echo "| R-01 (read) | ${R01_STATUS} | [R-01.log](R-01.log) / [R-01.json](R-01.json) |"
  echo "| W-01 (write→visible) | ${W01_STATUS} | [W-01.log](W-01.log) / [W-01.json](W-01.json) |"
  echo ""
  if [[ -f "${R01_JSON}" ]]; then
    echo "## R-01"
    echo ""
    echo '```json'
    cat "${R01_JSON}"
    echo ""
    echo '```'
    echo ""
  fi
  if [[ -f "${W01_JSON}" ]]; then
    echo "## W-01"
    echo ""
    echo '```json'
    cat "${W01_JSON}"
    echo ""
    echo '```'
    echo ""
  fi
  echo "## agent_runner tail"
  echo ""
  echo '```'
  tail -40 "${AGENT_LOG}" || true
  echo '```'
} > "${SUMMARY}"

echo "[INFO] done: ${SUMMARY}"
