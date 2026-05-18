#!/usr/bin/env bash
# 本地一次性 /  occasional 初始化：依赖、配置模板、MemPalace 宫殿、NATS JetStream 自检。
# 在首次跑 deploy/dev/run_all.sh 或 admin/run_all.sh 之前执行。
#
#   ./deploy/dev/init.sh
#   ./deploy/dev/init.sh --quick          # 仅确保 Chroma collection 存在（快）
#   ./deploy/dev/init.sh --full           # 完整 mempalace init（默认）
#   ./deploy/dev/init.sh --skip-sync      # 不跑 uv sync
#   ./deploy/dev/init.sh --with-admin     # 额外 uv sync --extra admin
#   ./deploy/dev/init.sh --skip-warm      # 跳过 ONNX 模型 / closets / 搜索预热
#   ./deploy/dev/init.sh --warm-all-wings # 对每个 wing 做一次搜索 dry-run
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
[[ -f "$REPO_ROOT/pyproject.toml" ]] || {
  echo "[ERROR] 无法解析仓库根目录（预期本脚本位于 <repo>/deploy/dev/init.sh）。" >&2
  exit 1
}

CFG_DIR="${REPO_ROOT}/eidolon/memory/config"
LOCAL_YAML="${CFG_DIR}/memory.default.yaml"
EXAMPLE_YAML="${CFG_DIR}/memory.default.yaml.example"

MODE="full"
DO_SYNC=1
WITH_ADMIN=0
DO_WARM=1
WARM_ALL_WINGS=0

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "选项:"
  echo "  --quick        仅创建 mempalace_drawers collection（不跑完整 init）"
  echo "  --full         完整 mempalace init（默认）"
  echo "  --skip-sync    跳过 uv sync"
  echo "  --with-admin   同步安装 admin 可选依赖（Admin UI）"
  echo "  --skip-warm    跳过 Chroma ONNX / closets / 搜索预热"
  echo "  --warm-all-wings  预热时对每个 wing 做搜索 dry-run（较慢）"
  echo "  -h, --help     显示帮助"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) MODE="quick" ;;
    --full) MODE="full" ;;
    --skip-sync) DO_SYNC=0 ;;
    --with-admin) WITH_ADMIN=1 ;;
    --skip-warm) DO_WARM=0 ;;
    --warm-all-wings) WARM_ALL_WINGS=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] 未知参数: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

unset VIRTUAL_ENV

if ! command -v uv >/dev/null 2>&1; then
  error "未找到 uv，请先安装: https://docs.astral.sh/uv/"
  exit 1
fi

cd "$REPO_ROOT"

if [[ ! -f "$LOCAL_YAML" ]]; then
  if [[ -f "$EXAMPLE_YAML" ]]; then
    cp "$EXAMPLE_YAML" "$LOCAL_YAML"
    info "已创建本地配置: ${LOCAL_YAML}"
    info "请按需编辑 runtime.palace_path、llm、nats 等项。"
  else
    warn "未找到 ${EXAMPLE_YAML}，跳过配置复制。"
  fi
else
  info "本地配置已存在: ${LOCAL_YAML}"
fi

if [[ "$DO_SYNC" == 1 ]]; then
  info "安装 Python 依赖 (uv sync --extra dev)…"
  uv sync --extra dev
  if [[ "$WITH_ADMIN" == 1 ]]; then
    info "安装 Admin 依赖 (uv sync --extra admin)…"
    uv sync --extra admin
  fi
fi

info "有效配置:"
uv run python "${REPO_ROOT}/scripts/live_config_check.py"

PALACE="$(uv run python -c "
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory
print(resolve_palace_directory(get_memory_settings()))
")"

info "MemPalace 宫殿目录: ${PALACE}"
mkdir -p "${PALACE}"

collection_exists() {
  uv run python -c "
import sys
from pathlib import Path
import chromadb
p = Path('${PALACE}')
if not p.is_dir():
    sys.exit(1)
client = chromadb.PersistentClient(path=str(p))
names = {c.name for c in client.list_collections()}
sys.exit(0 if 'mempalace_drawers' in names else 1)
" 2>/dev/null
}

if collection_exists; then
  info "Chroma collection mempalace_drawers 已存在。"
  if [[ "$MODE" == "full" ]]; then
    warn "若需重新扫描实体/重建索引，请手动: uv run mempalace init --yes --no-llm \"${PALACE}\""
  fi
else
  case "$MODE" in
    quick)
      info "快速初始化: 创建 mempalace_drawers…"
      uv run python -c "
from mempalace.palace import get_collection
get_collection('${PALACE}', create=True)
print('mempalace_drawers ready')
"
      ;;
    full)
      info "完整初始化: mempalace init（可能需数分钟）…"
      if ! uv run mempalace init --yes --no-llm "${PALACE}"; then
        error "mempalace init 失败；可改用: $0 --quick"
        exit 1
      fi
      ;;
  esac
fi

if collection_exists; then
  COUNT="$(uv run python -c "
from mempalace.palace import get_collection
print(get_collection('${PALACE}', create=False).count())
" 2>/dev/null || echo "?")"
  info "mempalace_drawers 就绪，当前 drawer 数量: ${COUNT}"
else
  error "初始化后仍未找到 mempalace_drawers。"
  exit 1
fi

if [[ "$DO_WARM" == 1 ]]; then
  info "预热运行时资源（Chroma ONNX ~79MB、mempalace_closets、搜索路径）…"
  if [[ "$WARM_ALL_WINGS" == 1 ]]; then
    warm_cmd=(uv run python "${REPO_ROOT}/scripts/warm_dev_runtime.py" --all-wings)
  else
    warm_cmd=(uv run python "${REPO_ROOT}/scripts/warm_dev_runtime.py")
  fi
  if ! "${warm_cmd[@]}"; then
    error "运行时预热失败。可稍后重试，或使用: $0 --skip-warm"
    exit 1
  fi
else
  warn "已跳过运行时预热（--skip-warm）。首次搜索可能仍需下载 ~/.cache/chroma/onnx_models/。"
fi

info "检查 NATS JetStream（${PALACE} 不依赖此项，但 worker 需要）…"
if uv run python -c "
import asyncio, nats, sys
async def main():
    nc = await nats.connect('nats://127.0.0.1:4222')
    await nc.jetstream().account_info()
    await nc.close()
asyncio.run(main())
" 2>/dev/null; then
  info "NATS JetStream 可用 (nats://127.0.0.1:4222)。"
else
  warn "NATS 未启用 JetStream 或未监听 4222。"
  warn "启动示例: mkdir -p /tmp/eidolon-nats-js && nats-server -js -p 4222 -sd /tmp/eidolon-nats-js"
fi

echo ""
info "初始化完成。下一步:"
echo "  ./deploy/dev/run_all.sh start    # worker + MCP HTTP"
echo "  ./admin/run_all.sh               # Admin UI（可选，需 Node）"
