#!/usr/bin/env bash
# 运行需要「真实 MemPalace Python 包」的测试（-m mempalace）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

export EIDOLON_MEMORY_RUN_LIVE=1
exec .venv/bin/python -m pytest tests -m mempalace -v --tb=short "$@"
