#!/usr/bin/env bash
# 启动 / 停止 FastAPI Admin（默认 8010）与 Vue dev（默认 5280）。
# 依赖：uv（含 --extra admin）、Node/npm。读路径需 supervisor / eidolon-memory-agent 已起（users.yaml 端口）。
#
#   ./admin/run_all.sh          # 前台启动（Ctrl+C 结束）
#   ./admin/run_all.sh start    # 后台启动
#   ./admin/run_all.sh stop
#   ./admin/run_all.sh status
#
# 端口：EIDOLON_MEMORY_ADMIN_PORT / VITE_FRONT_PORT
# 可选：EIDOLON_MEMORY_ADMIN_TOKEN（frontend 见 admin/web/.env.development）
set -euo pipefail

unset VIRTUAL_ENV

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACK_PORT="${EIDOLON_MEMORY_ADMIN_PORT:-8010}"
FRONT_PORT="${VITE_FRONT_PORT:-5280}"
export PYTHONPATH="${ROOT}/admin/server"

LOG_DIR="${ROOT}/logs"
PID_FILE="${LOG_DIR}/eidolon_admin_services.pids"
BACK_LOG="${LOG_DIR}/eidolon_admin_api.log"
FRONT_LOG="${LOG_DIR}/eidolon_admin_vite.log"

mkdir -p "$LOG_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

command -v uv >/dev/null || {
  error "需要安装 uv: https://docs.astral.sh/uv/"
  exit 1
}
command -v npm >/dev/null || {
  error "需要安装 Node.js / npm"
  exit 1
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

do_foreground() {
  if ! mcp_http_ready; then
    error "agent MCP HTTP 不可用。请先启动 supervisor 或 eidolon-memory-agent（见 users.yaml 端口）。"
    exit 1
  fi

  cleanup() {
    if [[ -n "${BACK_PID:-}" ]]; then
      kill "${BACK_PID}" 2>/dev/null || true
    fi
    if [[ -n "${FRONT_PID:-}" ]]; then
      kill "${FRONT_PID}" 2>/dev/null || true
    fi
  }
  trap cleanup EXIT INT TERM

  uv sync --extra admin --extra dev >/dev/null
  uv run uvicorn main:app --app-dir "${ROOT}/admin/server" --host 127.0.0.1 --port "${BACK_PORT}" &
  BACK_PID=$!

  if [[ ! -d "${ROOT}/admin/web/node_modules" ]]; then
    (cd "${ROOT}/admin/web" && npm install)
  fi

  (cd "${ROOT}/admin/web" && npm run dev -- --port "${FRONT_PORT}" --strictPort) &
  FRONT_PID=$!

  echo ""
  echo "Eidolon Memory Admin"
  echo "  API:       http://127.0.0.1:${BACK_PORT}/docs"
  echo "  前端(dev): http://127.0.0.1:${FRONT_PORT}/"
  echo "Ctrl+C 结束前后端。"
  echo ""

  wait "${BACK_PID}" "${FRONT_PID}" || true
}

mcp_http_ready() {
  cd "$ROOT"
  PYTHONPATH="${ROOT}/admin/server" uv run python -c "
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

do_start() {
  if [[ -f "$PID_FILE" ]] && any_alive; then
    error "Admin 已在运行（见 $PID_FILE）。先执行: $0 stop"
    exit 1
  fi
  clear_stale_pid_file || true

  if ! mcp_http_ready; then
    error "agent MCP HTTP 不可用。请先启动 supervisor 或 eidolon-memory-agent（users.yaml 首个 enabled 用户端口）。"
    error "若已启动仍失败，检查 HTTP_PROXY 是否未排除 localhost（MCP 客户端 trust_env=False）。"
    exit 1
  fi

  uv sync --extra admin --extra dev >/dev/null

  if [[ ! -d "${ROOT}/admin/web/node_modules" ]]; then
    (cd "${ROOT}/admin/web" && npm install)
  fi

  local tmp
  tmp="$(mktemp)"

  info "启动 Admin API: uvicorn :${BACK_PORT}"
  info "api 日志: $BACK_LOG"
  nohup uv run uvicorn main:app --app-dir "${ROOT}/admin/server" --host 127.0.0.1 --port "${BACK_PORT}" >>"$BACK_LOG" 2>&1 &
  echo "api=$!" >>"$tmp"

  info "启动 Vite: :${FRONT_PORT}"
  info "vite 日志: $FRONT_LOG"
  nohup bash -c "cd \"${ROOT}/admin/web\" && npm run dev -- --port \"${FRONT_PORT}\" --strictPort" >>"$FRONT_LOG" 2>&1 &
  echo "vite=$!" >>"$tmp"

  mv "$tmp" "$PID_FILE"

  info "已后台启动。PID 文件: $PID_FILE"
  echo "  API:       http://127.0.0.1:${BACK_PORT}/docs"
  echo "  前端(dev): http://127.0.0.1:${FRONT_PORT}/"
  info "停止: $0 stop"
}

do_stop() {
  if [[ ! -f "$PID_FILE" ]]; then
    info "无 PID 文件（未由此脚本后台启动）。"
    return 0
  fi

  info "停止 Admin API / Vite…"
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
  echo -e "${CYAN}==== eidolon-memory-admin ====${NC}"
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
    info "未运行或未由本脚本后台启动。"
    [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
  fi
  echo ""
  echo "  API:       http://127.0.0.1:${BACK_PORT}/docs"
  echo "  前端(dev): http://127.0.0.1:${FRONT_PORT}/"
  echo "  日志: $BACK_LOG , $FRONT_LOG"
}

case "${1:-}" in
  start)
    do_start
    ;;
  stop)
    do_stop
    ;;
  status)
    do_status
    ;;
  -h | --help | help)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  "")
    do_foreground
    ;;
  *)
    error "未知子命令: $1（支持: start | stop | status，或无参数前台运行）"
    exit 1
    ;;
esac
