#!/usr/bin/env bash
# 运行需要「真实 MemPalace MCP」的测试（-m mempalace）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if [[ -z "${EIDOLON_MEMORY_MCP_COMMAND:-}" ]]; then
  echo "请先设置 EIDOLON_MEMORY_MCP_COMMAND（MemPalace MCP 启动命令）。" >&2
  echo "示例见 tests/memory/test_live_mcp.py 顶部说明。" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run pytest tests -m mempalace -v --tb=short "$@"
else
  exec python3 -m pytest tests -m mempalace -v --tb=short "$@"
fi
