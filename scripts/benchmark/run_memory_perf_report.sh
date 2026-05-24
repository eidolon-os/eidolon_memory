#!/usr/bin/env bash
# Orchestrate D1 memory benchmarks: pytest + R-01 (recall) + W-01 (write→visible).
#
#   ./scripts/benchmark/run_memory_perf_report.sh
#   ./scripts/benchmark/run_memory_perf_report.sh --user-id bench --port 18030 --read-count 100
#
# Brings up a single agent_runner subprocess for ``--user-id``, runs the read
# load generator + the publish→recall-visible bench, then tears the agent down.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"

USER_ID="bench"
PORT="18030"
READ_COUNT="50"
WRITE_COUNT="5"
RUN_PYTEST="1"
RUN_WRITE="1"
SEED_SIZE=""   # "" = skip seed; S/M/L = seed N drawers
VOICE_FLAG=""

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "Options:"
  echo "  --user-id <id>     (default: bench)"
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
    --user-id) USER_ID="$2"; shift 2 ;;
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

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${REPO_ROOT}/reports/memory_perf_${STAMP}"
mkdir -p "$OUT_DIR"
echo "[INFO] output: $OUT_DIR"

GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo n/a)"
GIT_STATUS="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

# ----- pytest -----
PYTEST_LOG="${OUT_DIR}/pytest.log"
PYTEST_STATUS="skipped"
if [[ "$RUN_PYTEST" -eq 1 ]]; then
  echo "[INFO] pytest tests/ -q"
  if uv run pytest tests -q --tb=short 2>&1 | tee "$PYTEST_LOG"; then
    PYTEST_STATUS="PASS"
  else
    PYTEST_STATUS="FAIL"
  fi
fi

# ----- seed palace (optional) -----
if [[ -n "$SEED_SIZE" ]]; then
  # D1: resolve palace path via the same helper agent_runner uses; never hardcode.
  PALACE_DIR=$(uv run python -c "
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_for_user
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized
import sys
settings = get_memory_settings()
palace = resolve_palace_for_user(settings, sys.argv[1])
ensure_palace_initialized(sys.argv[1], palace)
print(palace)
" "$USER_ID")
  echo "[INFO] seeding palace ${PALACE_DIR} size=${SEED_SIZE}"
  uv run python scripts/benchmark/seed_palace.py \
    --palace "${PALACE_DIR}" --size "${SEED_SIZE}" 2>&1 | tee "${OUT_DIR}/seed.log"
fi

# ----- bring up agent_runner -----
AGENT_LOG="${OUT_DIR}/agent_runner.log"
echo "[INFO] starting eidolon-memory-agent --user-id=${USER_ID} --port=${PORT}"
uv run eidolon-memory-agent --user-id="${USER_ID}" --port="${PORT}" \
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
if uv run python scripts/benchmark/bench_read_livekit.py \
  --url "http://127.0.0.1:${PORT}/mcp" \
  --count "${READ_COUNT}" ${VOICE_FLAG} \
  --out "${R01_JSON}" 2>&1 | tee "${R01_LOG}"; then
  R01_STATUS="PASS"
else
  R01_STATUS="FAIL"
fi

# ----- W-01 -----
W01_JSON="${OUT_DIR}/W-01.json"
W01_LOG="${OUT_DIR}/W-01.log"
W01_STATUS="skipped"
if [[ "$RUN_WRITE" -eq 1 ]]; then
  echo "[INFO] W-01 write→visible bench (n=${WRITE_COUNT})"
  if uv run python scripts/benchmark/bench_write_jetstream.py \
    --count "${WRITE_COUNT}" \
    --user-id "${USER_ID}" \
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
  echo "- user_id: ${USER_ID}, port: ${PORT}"
  echo "- OMP_NUM_THREADS=${OMP_NUM_THREADS} MKL_NUM_THREADS=${MKL_NUM_THREADS}"
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
