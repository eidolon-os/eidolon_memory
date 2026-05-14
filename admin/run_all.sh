#!/usr/bin/env bash
# 同时启动 FastAPI Admin（默认 8010）与 Vue dev（默认 5280）。依赖：uv（含 `--extra admin`）、Node/npm。
# 端口：可用 EIDOLON_MEMORY_ADMIN_PORT / VITE_FRONT_PORT 重写。
# 可选：export EIDOLON_MEMORY_ADMIN_TOKEN=...（与后端一致时，frontend 需在 admin/web/.env.development 设 VITE_ADMIN_TOKEN）
set -euo pipefail

# 若外层激活了别的项目的 venv（如 eidolon-channel），uv 会报警；本仓库用 uv.lock 自带的 .venv。
unset VIRTUAL_ENV

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACK_PORT="${EIDOLON_MEMORY_ADMIN_PORT:-8010}"
FRONT_PORT="${VITE_FRONT_PORT:-5280}"
export PYTHONPATH="${ROOT}/admin/server"

cleanup() {
  if [[ -n "${BACK_PID:-}" ]]; then
    kill "${BACK_PID}" 2>/dev/null || true
  fi
  if [[ -n "${FRONT_PID:-}" ]]; then
    kill "${FRONT_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

command -v uv >/dev/null || {
  echo "需要安装 uv: https://docs.astral.sh/uv/"
  exit 1
}
command -v npm >/dev/null || {
  echo "需要安装 Node.js / npm"
  exit 1
}

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
