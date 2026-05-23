#!/usr/bin/env bash
# Start / stop / reload / status / restart for the D1 dev stack:
#   - eidolon-memory-supervisor (+ agent_runner children)
#   - eidolon-memory-discovery  (agent-routing HTTP)
#
# Admin UI lives in legacy/admin and is NOT managed by this script. To
# run it manually if needed, see legacy/admin/README.md.
#
#   ./deploy/dev/run_all.sh              # = start (supervisor + discovery)
#   ./deploy/dev/run_all.sh start
#   ./deploy/dev/run_all.sh stop
#   ./deploy/dev/run_all.sh restart      # stop all, then start all
#   ./deploy/dev/run_all.sh reload       # SIGHUP supervisor → re-read users.yaml
#   ./deploy/dev/run_all.sh status
#
# Single-component control:
#   ./deploy/dev/run_all.sh start-supervisor | stop-supervisor | status-supervisor
#   ./deploy/dev/run_all.sh start-discovery  | stop-discovery  | status-discovery
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

read_runtime_meta() {
  uv run python <<'PY'
import json, sys
from eidolon.memory.config.memory_settings import (
    get_memory_settings, resolve_log_dir, resolve_run_dir,
)
from eidolon.memory.config.users import (
    load_users_config, resolve_users_file_path,
)
s = get_memory_settings()
users_path = resolve_users_file_path(s)
try:
    cfg = load_users_config(s)
    enabled = [u.id for u in cfg.enabled_users()]
except Exception as exc:
    enabled = []
    sys.stderr.write(f"[warn] users.yaml parse failed: {exc}\n")
print(json.dumps({
    "log_dir": str(resolve_log_dir(s)),
    "run_dir": str(resolve_run_dir(s)),
    "users_file": str(users_path),
    "enabled_users": enabled,
    "discovery_host": s.discovery_http.host,
    "discovery_port": s.discovery_http.port,
    "discovery_path": s.discovery_http.path,
}))
PY
}

META_JSON="$(read_runtime_meta)"
LOG_DIR="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["log_dir"])')"
RUN_DIR="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["run_dir"])')"
USERS_FILE="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["users_file"])')"
ENABLED_USERS="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(",".join(json.load(sys.stdin)["enabled_users"]) or "(none)")')"
DISCOVERY_HOST="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["discovery_host"])')"
DISCOVERY_PORT="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["discovery_port"])')"
DISCOVERY_PATH="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["discovery_path"])')"

mkdir -p "$LOG_DIR" "$RUN_DIR"

SUP_LOG="${LOG_DIR}/supervisor.log"
SUP_PID="${RUN_DIR}/eidolon-memory-supervisor.pid"
SUP_CMD=(uv run eidolon-memory-supervisor)
DISCOVERY_LOG="${LOG_DIR}/discovery.log"
DISCOVERY_PID="${RUN_DIR}/eidolon-memory-discovery.pid"
DISCOVERY_CMD=(uv run eidolon-memory-discovery --host "${DISCOVERY_HOST}" --port "${DISCOVERY_PORT}")

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

read_sup_pid() { [[ -f "$SUP_PID" ]] && cat "$SUP_PID" 2>/dev/null || true; }
sup_alive() { local p; p="$(read_sup_pid)"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }
read_discovery_pid() { [[ -f "$DISCOVERY_PID" ]] && cat "$DISCOVERY_PID" 2>/dev/null || true; }
discovery_alive() { local p; p="$(read_discovery_pid)"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }

do_start_supervisor() {
  if sup_alive; then
    error "supervisor already running (PID $(read_sup_pid), see $SUP_PID). Use: $0 stop"
    exit 1
  fi
  [[ -f "$SUP_PID" ]] && rm -f "$SUP_PID"

  info "users.yaml: $USERS_FILE"
  info "enabled users: $ENABLED_USERS"
  info "log_dir: $LOG_DIR"
  info "run_dir: $RUN_DIR"
  info "launching: ${SUP_CMD[*]}"

  nohup "${SUP_CMD[@]}" >>"$SUP_LOG" 2>&1 &
  local sup_pid=$!
  echo "$sup_pid" >"$SUP_PID"
  sleep 1
  if ! kill -0 "$sup_pid" 2>/dev/null; then
    error "supervisor died immediately; tail of log:"
    tail -30 "$SUP_LOG" >&2 || true
    rm -f "$SUP_PID"
    exit 1
  fi
  info "supervisor PID=$sup_pid (pid=$SUP_PID, log=$SUP_LOG)"
}

do_stop_supervisor() {
  if ! sup_alive; then
    info "supervisor not running."
    [[ -f "$SUP_PID" ]] && rm -f "$SUP_PID"
    return 0
  fi
  local pid; pid="$(read_sup_pid)"
  info "SIGTERM supervisor PID=$pid"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
  done
  if kill -0 "$pid" 2>/dev/null; then
    warn "supervisor still alive after 30s; SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$SUP_PID"
  info "supervisor stopped."
}

do_reload_supervisor() {
  if ! sup_alive; then
    error "supervisor not running; cannot SIGHUP."
    exit 1
  fi
  local pid; pid="$(read_sup_pid)"
  info "SIGHUP supervisor PID=$pid (re-read users.yaml)"
  kill -HUP "$pid"
}

do_status_supervisor() {
  echo -e "${CYAN}==== eidolon-memory-supervisor ====${NC}"
  echo "  users.yaml:    $USERS_FILE"
  echo "  enabled:       $ENABLED_USERS"
  echo "  log_dir:       $LOG_DIR"
  echo "  run_dir:       $RUN_DIR"
  if sup_alive; then
    local pid; pid="$(read_sup_pid)"
    info "running: PID $pid"
    echo "  agent children (pgrep eidolon-memory-agent):"
    pgrep -fl 'eidolon-memory-agent' || echo "    (none)"
  else
    info "not running."
    [[ -f "$SUP_PID" ]] && rm -f "$SUP_PID"
  fi
  echo ""
  echo "  log tail:"
  if [[ -f "$SUP_LOG" ]]; then
    tail -10 "$SUP_LOG" | sed 's/^/    /'
  else
    echo "    (no supervisor.log yet)"
  fi
}

do_start_discovery() {
  if discovery_alive; then
    error "discovery already running (PID $(read_discovery_pid), see $DISCOVERY_PID). Use: $0 stop-discovery"
    exit 1
  fi
  [[ -f "$DISCOVERY_PID" ]] && rm -f "$DISCOVERY_PID"

  info "starting Discovery HTTP: http://${DISCOVERY_HOST}:${DISCOVERY_PORT}${DISCOVERY_PATH}"
  info "discovery log: $DISCOVERY_LOG"
  nohup "${DISCOVERY_CMD[@]}" >>"$DISCOVERY_LOG" 2>&1 &
  local discovery_pid=$!
  echo "$discovery_pid" >"$DISCOVERY_PID"
  sleep 1
  if ! kill -0 "$discovery_pid" 2>/dev/null; then
    error "discovery died immediately; tail of log:"
    tail -30 "$DISCOVERY_LOG" >&2 || true
    rm -f "$DISCOVERY_PID"
    exit 1
  fi
  info "discovery PID=$discovery_pid (pid=$DISCOVERY_PID, log=$DISCOVERY_LOG)"
}

do_stop_discovery() {
  if ! discovery_alive; then
    info "discovery not running."
    [[ -f "$DISCOVERY_PID" ]] && rm -f "$DISCOVERY_PID"
    return 0
  fi
  local pid; pid="$(read_discovery_pid)"
  info "SIGTERM discovery PID=$pid"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
  done
  if kill -0 "$pid" 2>/dev/null; then
    warn "discovery still alive after 10s; SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$DISCOVERY_PID"
  info "discovery stopped."
}

do_status_discovery() {
  echo -e "${CYAN}==== eidolon-memory-discovery ====${NC}"
  echo "  endpoint: http://${DISCOVERY_HOST}:${DISCOVERY_PORT}${DISCOVERY_PATH}"
  if discovery_alive; then
    local pid; pid="$(read_discovery_pid)"
    info "running: PID $pid"
  else
    info "not running."
    [[ -f "$DISCOVERY_PID" ]] && rm -f "$DISCOVERY_PID"
  fi
  echo ""
  echo "  log tail:"
  if [[ -f "$DISCOVERY_LOG" ]]; then
    tail -10 "$DISCOVERY_LOG" | sed 's/^/    /'
  else
    echo "    (no discovery.log yet)"
  fi
}

do_start() {
  do_start_supervisor
  do_start_discovery
}

do_stop() {
  do_stop_discovery
  do_stop_supervisor
}

do_restart() {
  do_stop
  sleep 1
  do_start
}

do_status() {
  do_status_supervisor
  echo ""
  do_status_discovery
}

CMD="${1:-start}"
case "$CMD" in
  start|"") do_start ;;
  stop) do_stop ;;
  restart) do_restart ;;
  reload) do_reload_supervisor ;;
  status) do_status ;;
  start-supervisor) do_start_supervisor ;;
  stop-supervisor) do_stop_supervisor ;;
  status-supervisor) do_status_supervisor ;;
  start-discovery) do_start_discovery ;;
  stop-discovery) do_stop_discovery ;;
  status-discovery) do_status_discovery ;;
  *)
    error "unknown command: $CMD"
    echo "Usage: $0 {start|stop|restart|reload|status|start-supervisor|stop-supervisor|status-supervisor|start-discovery|stop-discovery|status-discovery}" >&2
    exit 1
    ;;
esac
