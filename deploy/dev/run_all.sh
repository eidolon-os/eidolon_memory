#!/usr/bin/env bash
# Start / stop / reload / status for ``eidolon-memory-supervisor`` (D1).
#
# 入口语义：supervisor 读 users.yaml，自己 spawn N 个 agent_runner。本脚本只是
# 把 supervisor 后台跑起来并管理它的 PID。要调试单个 agent，用
# ``./deploy/dev/run_single_agent.sh``，不要往这里塞单 agent 模式。
#
# 运行期路径由 memory.default.yaml 的 runtime.log_dir / runtime.run_dir 决定
# （默认 ~/eidolon/logs 与 ~/eidolon/run），可通过环境变量
# ``EIDOLON_MEMORY_LOG_DIR`` / ``EIDOLON_MEMORY_RUN_DIR`` 覆盖。
# 主配置文件位置由 ``EIDOLON_MEMORY_SETTINGS_YAML`` 决定（不设则用仓库默认）。
#
#   ./deploy/dev/run_all.sh           # = start
#   ./deploy/dev/run_all.sh start
#   ./deploy/dev/run_all.sh stop
#   ./deploy/dev/run_all.sh reload    # SIGHUP supervisor → 重读 users.yaml
#   ./deploy/dev/run_all.sh status
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

# Read runtime dirs and user count from the active settings YAML (single uv
# invocation = a fixed startup cost).
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
}))
PY
}

META_JSON="$(read_runtime_meta)"
LOG_DIR="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["log_dir"])')"
RUN_DIR="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["run_dir"])')"
USERS_FILE="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["users_file"])')"
ENABLED_USERS="$(echo "$META_JSON" | uv run python -c 'import json,sys;print(",".join(json.load(sys.stdin)["enabled_users"]) or "(none)")')"

mkdir -p "$LOG_DIR" "$RUN_DIR"

SUP_LOG="${LOG_DIR}/supervisor.log"
SUP_PID="${RUN_DIR}/eidolon-memory-supervisor.pid"
SUP_CMD=(uv run eidolon-memory-supervisor)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

read_pid() { [[ -f "$SUP_PID" ]] && cat "$SUP_PID" 2>/dev/null || true; }
pid_alive() { local p; p="$(read_pid)"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }

do_start() {
  if pid_alive; then
    error "supervisor already running (PID $(read_pid), see $SUP_PID). Use: $0 stop"
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

do_stop() {
  if ! pid_alive; then
    info "supervisor not running."
    [[ -f "$SUP_PID" ]] && rm -f "$SUP_PID"
    return 0
  fi
  local pid; pid="$(read_pid)"
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
  info "stopped."
}

do_reload() {
  if ! pid_alive; then
    error "supervisor not running; cannot SIGHUP."
    exit 1
  fi
  local pid; pid="$(read_pid)"
  info "SIGHUP supervisor PID=$pid (re-read users.yaml)"
  kill -HUP "$pid"
}

do_status() {
  echo -e "${CYAN}==== eidolon-memory-supervisor ====${NC}"
  echo "  users.yaml:    $USERS_FILE"
  echo "  enabled:       $ENABLED_USERS"
  echo "  log_dir:       $LOG_DIR"
  echo "  run_dir:       $RUN_DIR"
  if pid_alive; then
    local pid; pid="$(read_pid)"
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

case "${1:-start}" in
  start|"") do_start ;;
  stop)    do_stop ;;
  reload)  do_reload ;;
  status)  do_status ;;
  restart) do_stop; do_start ;;
  *)
    echo "usage: $0 [start|stop|reload|status|restart]" >&2
    exit 1
    ;;
esac
