#!/usr/bin/env bash
# Start / stop / reload / status / restart for D1 dev stack:
#   - eidolon-memory-supervisor (+ agent_runner children)
#   - Eidolon Memory Admin (FastAPI + Vite dev)
#
#   ./deploy/dev/run_all.sh              # = start (supervisor + admin)
#   ./deploy/dev/run_all.sh start
#   ./deploy/dev/run_all.sh stop
#   ./deploy/dev/run_all.sh restart      # stop all, then start all
#   ./deploy/dev/run_all.sh reload       # SIGHUP supervisor → re-read users.yaml
#   ./deploy/dev/run_all.sh status
#
# Admin-only:
#   ./deploy/dev/run_all.sh start-admin | stop-admin | restart-admin | status-admin
#   ./deploy/dev/run_all.sh foreground-admin   # API + Vite in foreground (Ctrl+C)
#
# Discovery-only:
#   ./deploy/dev/run_all.sh start-discovery | stop-discovery | status-discovery
#
# Supervisor-only:
#   ./deploy/dev/run_all.sh start-supervisor | stop-supervisor | status-supervisor
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

export PYTHONPATH="${REPO_ROOT}/admin/server"
ADMIN_BACK_PORT="${EIDOLON_MEMORY_ADMIN_PORT:-8010}"
ADMIN_FRONT_PORT="${VITE_FRONT_PORT:-5280}"
ADMIN_PID="${RUN_DIR}/eidolon_admin_services.pids"
ADMIN_BACK_LOG="${LOG_DIR}/eidolon_admin_api.log"
ADMIN_FRONT_LOG="${LOG_DIR}/eidolon_admin_vite.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

read_sup_pid() { [[ -f "$SUP_PID" ]] && cat "$SUP_PID" 2>/dev/null || true; }
sup_alive() { local p; p="$(read_sup_pid)"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }
read_discovery_pid() { [[ -f "$DISCOVERY_PID" ]] && cat "$DISCOVERY_PID" 2>/dev/null || true; }
discovery_alive() { local p; p="$(read_discovery_pid)"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }

admin_any_alive() {
  [[ -f "$ADMIN_PID" ]] || return 1
  local line pid
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  done <"$ADMIN_PID"
  return 1
}

admin_clear_stale_pid_file() {
  [[ -f "$ADMIN_PID" ]] || return 0
  if admin_any_alive; then
    return 1
  fi
  rm -f "$ADMIN_PID"
  return 0
}

mcp_http_ready() {
  PYTHONPATH="${REPO_ROOT}/admin/server" uv run python -c "
import asyncio, sys
from eidolon.memory.config.memory_settings import get_memory_settings
from mcp_client import mcp_http_url, probe_mcp_http
from user_registry import list_enabled_users

async def main() -> bool:
    settings = get_memory_settings()
    users = list_enabled_users(settings)
    if not users:
        return False
    entry = users[0]
    url = mcp_http_url(settings, port=entry.port)
    return await probe_mcp_http(url, settings=settings)

sys.exit(0 if asyncio.run(main()) else 1)
" 2>/dev/null
}

wait_mcp_http_ready() {
  local i
  for i in $(seq 1 40); do
    if mcp_http_ready; then
      return 0
    fi
    sleep 1
  done
  return 1
}

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

do_start_admin() {
  if ! command -v npm >/dev/null 2>&1; then
    error "npm not on PATH; cannot start Admin UI"
    exit 1
  fi

  if [[ -f "$ADMIN_PID" ]] && admin_any_alive; then
    error "admin already running (see $ADMIN_PID). Use: $0 stop-admin"
    exit 1
  fi
  admin_clear_stale_pid_file || true

  if ! wait_mcp_http_ready; then
    error "agent MCP HTTP not ready (start supervisor first; check users.yaml ports)."
    error "If already started, check HTTP_PROXY excludes localhost."
    exit 1
  fi

  uv sync --extra admin --extra dev >/dev/null

  if [[ ! -d "${REPO_ROOT}/admin/web/node_modules" ]]; then
    (cd "${REPO_ROOT}/admin/web" && npm install)
  fi

  local tmp
  tmp="$(mktemp)"

  info "starting Admin API: uvicorn :${ADMIN_BACK_PORT}"
  info "api log: $ADMIN_BACK_LOG"
  nohup uv run uvicorn main:app --app-dir "${REPO_ROOT}/admin/server" \
    --host 127.0.0.1 --port "${ADMIN_BACK_PORT}" >>"$ADMIN_BACK_LOG" 2>&1 &
  echo "api=$!" >>"$tmp"

  info "starting Vite: :${ADMIN_FRONT_PORT}"
  info "vite log: $ADMIN_FRONT_LOG"
  nohup bash -c "cd \"${REPO_ROOT}/admin/web\" && npm run dev -- --port \"${ADMIN_FRONT_PORT}\" --strictPort" \
    >>"$ADMIN_FRONT_LOG" 2>&1 &
  echo "vite=$!" >>"$tmp"

  mv "$tmp" "$ADMIN_PID"

  info "admin started (pid file: $ADMIN_PID)"
  echo "  API:       http://127.0.0.1:${ADMIN_BACK_PORT}/docs"
  echo "  frontend:  http://127.0.0.1:${ADMIN_FRONT_PORT}/"
}

do_stop_admin() {
  if [[ ! -f "$ADMIN_PID" ]]; then
    info "admin not running (no pid file)."
    return 0
  fi

  info "stopping Admin API / Vite…"
  local line pid key
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      info "  SIGTERM $key (PID $pid)"
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done <"$ADMIN_PID"

  sleep 2

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      warn "  SIGKILL $key (PID $pid)"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done <"$ADMIN_PID"

  rm -f "$ADMIN_PID"
  info "admin stopped."
}

do_foreground_admin() {
  if ! command -v npm >/dev/null 2>&1; then
    error "npm not on PATH; cannot start Admin UI"
    exit 1
  fi
  if ! wait_mcp_http_ready; then
    error "agent MCP HTTP not ready (run: $0 start-supervisor or $0 start)"
    exit 1
  fi

  cleanup() {
    [[ -n "${BACK_PID:-}" ]] && kill "${BACK_PID}" 2>/dev/null || true
    [[ -n "${FRONT_PID:-}" ]] && kill "${FRONT_PID}" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  uv sync --extra admin --extra dev >/dev/null
  if [[ ! -d "${REPO_ROOT}/admin/web/node_modules" ]]; then
    (cd "${REPO_ROOT}/admin/web" && npm install)
  fi

  uv run uvicorn main:app --app-dir "${REPO_ROOT}/admin/server" \
    --host 127.0.0.1 --port "${ADMIN_BACK_PORT}" &
  BACK_PID=$!
  (cd "${REPO_ROOT}/admin/web" && npm run dev -- --port "${ADMIN_FRONT_PORT}" --strictPort) &
  FRONT_PID=$!

  echo ""
  echo "Eidolon Memory Admin (foreground)"
  echo "  API:       http://127.0.0.1:${ADMIN_BACK_PORT}/docs"
  echo "  frontend:  http://127.0.0.1:${ADMIN_FRONT_PORT}/"
  echo "Ctrl+C to stop. Background: $0 start-admin"
  echo ""
  wait "${BACK_PID}" "${FRONT_PID}" || true
}

do_status_admin() {
  echo -e "${CYAN}==== eidolon-memory-admin ====${NC}"
  if [[ -f "$ADMIN_PID" ]] && admin_any_alive; then
    info "running:"
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      key="${line%%=*}"
      pid="${line#*=}"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "  ✓ ${key}: PID $pid"
      else
        echo "  ✗ ${key}: PID $pid (stale)"
      fi
    done <"$ADMIN_PID"
  else
    info "not running."
    [[ -f "$ADMIN_PID" ]] && rm -f "$ADMIN_PID"
  fi
  echo ""
  echo "  API:       http://127.0.0.1:${ADMIN_BACK_PORT}/docs"
  echo "  frontend:  http://127.0.0.1:${ADMIN_FRONT_PORT}/"
  echo "  logs:      $ADMIN_BACK_LOG , $ADMIN_FRONT_LOG"
}

do_start() {
  do_start_supervisor
  do_start_discovery
  if command -v npm >/dev/null 2>&1; then
    do_start_admin
  else
    warn "npm not found; supervisor started without Admin UI"
  fi
}

do_stop() {
  do_stop_admin
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
  echo ""
  do_status_admin
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
  start-admin) do_start_admin ;;
  stop-admin) do_stop_admin ;;
  restart-admin) do_stop_admin; sleep 1; do_start_admin ;;
  status-admin) do_status_admin ;;
  foreground-admin) do_foreground_admin ;;
  -h|--help|help)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "usage: $0 [start|stop|restart|reload|status|start-admin|stop-admin|restart-admin|status-admin|foreground-admin|start-discovery|stop-discovery|status-discovery|start-supervisor|stop-supervisor|status-supervisor]" >&2
    exit 1
    ;;
esac
