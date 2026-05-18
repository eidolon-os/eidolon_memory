#!/usr/bin/env bash
# Orchestrate PoC gates, pytest, and memory performance benchmarks.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"

PALACE_SIZE="M"
DURATION_READ=30
DURATION_WRITE=60
DURATION_MIXED=120
RUN_POC=1
RUN_PYTEST=1
FULL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --palace-size) PALACE_SIZE="$2"; shift 2 ;;
    --duration-read) DURATION_READ="$2"; shift 2 ;;
    --duration-write) DURATION_WRITE="$2"; shift 2 ;;
    --duration-mixed) DURATION_MIXED="$2"; shift 2 ;;
    --skip-poc) RUN_POC=0; shift ;;
    --skip-pytest) RUN_PYTEST=0; shift ;;
    --full) FULL=1; shift ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${REPO_ROOT}/reports/memory_perf_${STAMP}"
mkdir -p "$OUT_DIR" "${REPO_ROOT}/reports"

echo "[INFO] output: $OUT_DIR"

if [[ "$RUN_POC" -eq 1 ]]; then
  echo "[INFO] Phase 0 PoC: Chroma reload"
  uv run python scripts/poc_chroma_reload_latency.py \
    --out "${REPO_ROOT}/reports/poc_chroma_reload.json" || true
  echo "[INFO] Phase 0 PoC: SQLite RW"
  uv run python scripts/poc_sqlite_rw_concurrent.py \
    --duration 30 \
    --out "${REPO_ROOT}/reports/poc_sqlite_rw.json" || true
fi

if [[ "$RUN_PYTEST" -eq 1 ]]; then
  echo "[INFO] pytest tests/memory"
  uv run pytest tests/memory -q --tb=short 2>&1 | tee "${OUT_DIR}/pytest.log"
fi

echo "[INFO] LiveKit read benchmark (R-01)"
uv run python scripts/benchmark/bench_read_livekit.py \
  --duration "$DURATION_READ" \
  --qps 2 2>&1 | tee "${OUT_DIR}/R-01.log" || true

if [[ "$FULL" -eq 1 ]]; then
  echo "[INFO] JetStream write benchmark (W-01) — requires worker running"
  uv run python scripts/benchmark/bench_write_jetstream.py \
    --count 20 2>&1 | tee "${OUT_DIR}/W-01.log" || true
fi

cp -f "${REPO_ROOT}/reports/poc_chroma_reload.json" "${OUT_DIR}/" 2>/dev/null || true
cp -f "${REPO_ROOT}/reports/poc_sqlite_rw.json" "${OUT_DIR}/" 2>/dev/null || true

cat > "${OUT_DIR}/summary.md" <<EOF
# Eidolon Memory 性能报告

- 时间: ${STAMP}
- 输出目录: ${OUT_DIR}
- OMP_NUM_THREADS: ${OMP_NUM_THREADS}

## 说明

- R-01: 见 \`R-01.log\`（LiveKit in-process recall）
- PoC: \`poc_chroma_reload.json\`, \`poc_sqlite_rw.json\`
- pytest: \`pytest.log\`

完整自动化表格见后续 \`metrics.json\` 迭代。
EOF

echo "[INFO] done: ${OUT_DIR}/summary.md"
