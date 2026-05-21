#!/usr/bin/env bash
# Eidolon Memory Admin — foreground dev, or delegate start/stop to deploy/dev/run_all.sh.
#
#   ./admin/run_all.sh              # 前台启动 API + Vite（Ctrl+C 结束）
#   ./admin/run_all.sh start        # 后台启动（同 deploy/dev/run_all.sh start-admin）
#   ./admin/run_all.sh stop         # 后台停止
#   ./admin/run_all.sh restart      # 后台重启 Admin
#   ./admin/run_all.sh status
#
# 一键启停 supervisor + admin：请用 ./deploy/dev/run_all.sh
#
set -euo pipefail

unset VIRTUAL_ENV

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="${ROOT}/deploy/dev/run_all.sh"

BACK_PORT="${EIDOLON_MEMORY_ADMIN_PORT:-8010}"
FRONT_PORT="${VITE_FRONT_PORT:-5280}"
export PYTHONPATH="${ROOT}/admin/server"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

command -v uv >/dev/null || {
  error "需要安装 uv: https://docs.astral.sh/uv/"
  exit 1
}
command -v npm >/dev/null || {
  error "需要安装 Node.js / npm"
  exit 1
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

do_foreground() {
  if ! mcp_http_ready; then
    error "agent MCP HTTP 不可用。请先: ${DEPLOY} start  或 start-supervisor"
    exit 1
  fi

  cleanup() {
    [[ -n "${BACK_PID:-}" ]] && kill "${BACK_PID}" 2>/dev/null || true
    [[ -n "${FRONT_PID:-}" ]] && kill "${FRONT_PID}" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  cd "$ROOT"
  uv sync --extra admin --extra dev >/dev/null
  uv run uvicorn main:app --app-dir "${ROOT}/admin/server" --host 127.0.0.1 --port "${BACK_PORT}" &
  BACK_PID=$!

  if [[ ! -d "${ROOT}/admin/web/node_modules" ]]; then
    (cd "${ROOT}/admin/web" && npm install)
  fi

  (cd "${ROOT}/admin/web" && npm run dev -- --port "${FRONT_PORT}" --strictPort) &
  FRONT_PID=$!

  echo ""
  echo "Eidolon Memory Admin (foreground)"
  echo "  API:       http://127.0.0.1:${BACK_PORT}/docs"
  echo "  前端(dev): http://127.0.0.1:${FRONT_PORT}/"
  echo "Ctrl+C 结束。后台模式: $0 start"
  echo ""

  wait "${BACK_PID}" "${FRONT_PID}" || true
}

case "${1:-}" in
  start)
    exec "$DEPLOY" start-admin
    ;;
  stop)
    exec "$DEPLOY" stop-admin
    ;;
  restart)
    exec "$DEPLOY" restart-admin
    ;;
  status)
    exec "$DEPLOY" status-admin
    ;;
  -h | --help | help)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  "")
    do_foreground
    ;;
  *)
    error "未知子命令: $1（支持: start | stop | restart | status，或无参数前台运行）"
    exit 1
    ;;
esac
