#!/usr/bin/env bash
# 启动 / 停止：JetStream memory worker + MCP read server（均后台）。
# 均使用 uv run python -m ...，不要求 pip install -e .。
#
#   ./deploy/dev/run_all.sh
#   ./deploy/dev/run_all.sh stop
#   ./deploy/dev/run_all.sh status
#
# Worker:  uv sync --extra dev
# MCP:     需 additionally  uv sync --extra mcp  （否则会跳过 MCP 并告警）
#
# MCP 为 stdio 协议，常见用法仍是由 Cursor/宿主进程单独 spawn；
# 本脚本里的 MCP nohup 便于本机冒烟；若stdin无连接也可能很快退出，请看 logs。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
[[ -f "$REPO_ROOT/pyproject.toml" ]] || {
  echo "[ERROR] 无法解析仓库根目录（预期本脚本位于 <repo>/deploy/dev/run_all.sh）。" >&2
  exit 1
}

LOG_DIR="$REPO_ROOT/logs"
PID_FILE="$LOG_DIR/eidolon_memory_services.pids"
mkdir -p "$LOG_DIR"

unset VIRTUAL_ENV

WORKER_LOG="${LOG_DIR}/eidolon_memory_worker.log"
MCP_LOG="${LOG_DIR}/eidolon_memory_mcp.log"
WORKER_CMD=(uv run python -m eidolon.memory.entrypoints.worker)
MCP_CMD=(uv run python -m eidolon.memory.entrypoints.mcp_server)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

mcp_optional_deps_ok() {
  (cd "$REPO_ROOT" && uv run python -c "import mcp" >/dev/null 2>&1)
}

any_alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local line pid
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  done <"$PID_FILE"
  return 1
}

clear_stale_pid_file() {
  [[ -f "$PID_FILE" ]] || return 0
  if any_alive; then
    return 1
  fi
  rm -f "$PID_FILE"
  return 0
}

do_start() {
  cd "$REPO_ROOT"

  if ! command -v uv >/dev/null 2>&1; then
    error "未找到 uv，请先安装: https://docs.astral.sh/uv/"
    exit 1
  fi

  if [[ -f "$PID_FILE" ]] && any_alive; then
    error "进程已在运行（见 $PID_FILE）。先执行: $0 stop"
    exit 1
  fi
  clear_stale_pid_file || true

  local tmp
  tmp="$(mktemp)"

  info "启动 worker: ${WORKER_CMD[*]}"
  info "worker 日志: $WORKER_LOG"
  nohup "${WORKER_CMD[@]}" >>"$WORKER_LOG" 2>&1 &
  echo "worker=$!" >>"$tmp"

  if [[ "${SKIP_MCP:-}" == "1" ]]; then
    warn "已设 SKIP_MCP=1，跳过 MCP。"
  elif mcp_optional_deps_ok; then
    info "启动 MCP: ${MCP_CMD[*]}"
    info "mcp 日志: $MCP_LOG"
    nohup "${MCP_CMD[@]}" >>"$MCP_LOG" 2>&1 &
    echo "mcp=$!" >>"$tmp"
  else
    warn "未检测到 mcp 包（请执行: cd $REPO_ROOT && uv sync --extra mcp）。仅启动 worker。"
  fi

  mv "$tmp" "$PID_FILE"

  info "已后台启动。PID 文件: $PID_FILE"
  info "停止: $0 stop"
}

do_stop() {
  cd "$REPO_ROOT"

  if [[ ! -f "$PID_FILE" ]]; then
    info "无 PID 文件（未由此脚本启动）。"
    return 0
  fi

  info "停止 worker / MCP…"
  local line pid key
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      info "  SIGTERM $key (PID $pid)"
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done <"$PID_FILE"

  sleep 2

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    pid="${line#*=}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      warn "  SIGKILL $key (PID $pid)"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done <"$PID_FILE"

  rm -f "$PID_FILE"
  info "已停止。"
}

do_status() {
  echo -e "${CYAN}==== eidolon-memory (worker + MCP) ====${NC}"
  if [[ -f "$PID_FILE" ]] && any_alive; then
    info "运行中:"
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      key="${line%%=*}"
      pid="${line#*=}"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "  ✓ ${key}: PID $pid"
      else
        echo "  ✗ ${key}: PID $pid (已失效)"
      fi
    done <"$PID_FILE"
  else
    info "未运行或未由本脚本启动。"
    [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
  fi
  echo ""
  printf '  worker 等价命令: uv run python -m eidolon.memory.entrypoints.worker\n'
  printf '  MCP   等价命令: uv run python -m eidolon.memory.entrypoints.mcp_server\n'
  echo "  日志: $WORKER_LOG , $MCP_LOG"
}

case "${1:-start}" in
  start|"")
    do_start
    ;;
  stop)
    do_stop
    ;;
  status)
    do_status
    ;;
  *)
    echo "用法: $0 [start|stop|status]" >&2
    exit 1
    ;;
esac
