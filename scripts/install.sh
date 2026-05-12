#!/usr/bin/env bash
# 一键安装本仓库常用运行时依赖：
#   - Python 环境 + optional ``mcp``（MemPalace MCP 客户端）
#   - 可选：MemPalace 官方 CLI/MCP（uv tool，供 EIDOLON_MEMORY_MCP_COMMAND）
#
# 用法：
#   ./scripts/install.sh
#   SKIP_MEMPALACE_TOOL=1 ./scripts/install.sh   # 不装 MemPalace tool
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"
echo "[eidolon-memory] 仓库根目录: ${REPO_ROOT}"

if command -v uv >/dev/null 2>&1; then
  echo "[eidolon-memory] uv sync --extra mcp …"
  uv sync --extra mcp
else
  echo "[eidolon-memory] 未检测到 uv，改用 pip editable + [mcp] …" >&2
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[eidolon-memory] 错误：需要 Python 3.11+ 与 pip，或安装 uv: https://docs.astral.sh/uv/" >&2
    exit 1
  fi
  python3 -m pip install -e ".[mcp]"
fi

if [[ "${SKIP_MEMPALACE_TOOL:-}" == "1" ]]; then
  echo "[eidolon-memory] 已跳过 MemPalace tool（SKIP_MEMPALACE_TOOL=1）。"
else
  if command -v uv >/dev/null 2>&1; then
    echo "[eidolon-memory] uv tool install mempalace（MemPalace MCP/CLI）…"
    if uv tool install mempalace; then
      echo "[eidolon-memory] MemPalace 已安装；请将 uv tool 的 bin 目录加入 PATH（常见为 ~/.local/bin）。"
    else
      echo "[eidolon-memory] 警告：MemPalace tool 安装失败，可稍后手动执行: uv tool install mempalace" >&2
    fi
  else
    echo "[eidolon-memory] 无 uv，跳过 MemPalace tool。可: pip install mempalace 或安装 uv 后重跑本脚本。" >&2
  fi
fi

echo ""
echo "[eidolon-memory] 安装步骤结束。"
echo "  · 配置 EIDOLON_MEMORY_MCP_COMMAND 指向 MemPalace MCP 启动命令（以 MemPalace 文档为准）。"
echo "  · JetStream 写路径需 nats-server（启用 JetStream）及 EIDOLON_MEMORY_JS_STREAM 等，见仓库根 README.md。"
