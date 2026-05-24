#!/usr/bin/env bash
# Internal helper: bring up a fresh user palace at a target size, run R-01 voice,
# tear down. Used by the scale-test orchestrator.
#
# Usage: _run_scale_one.sh <user_id> <port> <size_label> <count> <out_dir>
set -euo pipefail

USER_ID="$1"
PORT="$2"
SIZE="$3"       # S | M | L
COUNT="$4"
OUT_DIR="$5"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# D1 palace layout — must match config/palace_directory.resolve_palace_for_user.
PALACE="${EIDOLON_MEMORY_PALACES_ROOT:-$HOME/eidolon/memory/mempalaces}/${USER_ID}"

echo "[scale] ===== ${SIZE} on ${USER_ID}:${PORT} ====="
pkill -9 -f "eidolon-memory-agent --user-id ${USER_ID}" 2>/dev/null || true
sleep 1
rm -rf "$PALACE"

T0=$(date +%s.%N)
.venv/bin/python -c "
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_for_user
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized
s = get_memory_settings()
ensure_palace_initialized('${USER_ID}', resolve_palace_for_user(s, '${USER_ID}'))
" >/dev/null
T1=$(date +%s.%N)
INIT_S=$(awk "BEGIN {printf \"%.2f\", $T1 - $T0}")
echo "[scale] init: ${INIT_S}s"

T2=$(date +%s.%N)
.venv/bin/python scripts/benchmark/seed_palace.py --palace "$PALACE" --size "$SIZE" \
  > "$OUT_DIR/${SIZE}-seed.log" 2>&1
T3=$(date +%s.%N)
SEED_S=$(awk "BEGIN {printf \"%.2f\", $T3 - $T2}")
echo "[scale] seed ${SIZE}: ${SEED_S}s"

AGENT_LOG="$OUT_DIR/${SIZE}-agent.log"
T4=$(date +%s.%N)
.venv/bin/eidolon-memory-agent --user-id "${USER_ID}" --port "${PORT}" \
  > "$AGENT_LOG" 2>&1 &
APID=$!
trap "kill -9 $APID 2>/dev/null || true" RETURN

for i in $(seq 1 30); do
  nc -z 127.0.0.1 "$PORT" 2>/dev/null && break
  sleep 1
done
for i in $(seq 1 60); do
  grep -q "agent_runner_warm_complete" "$AGENT_LOG" 2>/dev/null && break
  sleep 1
done
T5=$(date +%s.%N)
READY_S=$(awk "BEGIN {printf \"%.2f\", $T5 - $T4}")
echo "[scale] agent ready: ${READY_S}s"

T6=$(date +%s.%N)
.venv/bin/python scripts/benchmark/bench_read_livekit.py \
  --url "http://127.0.0.1:${PORT}/mcp" --count "$COUNT" --voice \
  --out "$OUT_DIR/${SIZE}-R01.json" \
  > "$OUT_DIR/${SIZE}-R01.log" 2>&1
T7=$(date +%s.%N)
RUN_S=$(awk "BEGIN {printf \"%.2f\", $T7 - $T6}")
echo "[scale] R-01 wall: ${RUN_S}s"

kill -9 $APID 2>/dev/null || true
pkill -9 -P $APID 2>/dev/null || true
sleep 1

# Drop drawer count into a side file
DRAWERS=$(.venv/bin/python -c "
from mempalace.palace import get_collection
print(get_collection('$PALACE', create=False).count())
" 2>/dev/null || echo "?")

cat > "$OUT_DIR/${SIZE}-timing.json" <<JSON
{
  "size_label": "${SIZE}",
  "drawers": ${DRAWERS},
  "init_seconds": ${INIT_S},
  "seed_seconds": ${SEED_S},
  "agent_ready_seconds": ${READY_S},
  "r01_wall_seconds": ${RUN_S}
}
JSON
cat "$OUT_DIR/${SIZE}-timing.json"
