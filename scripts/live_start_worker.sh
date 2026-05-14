#!/usr/bin/env bash
# 启动 JetStream memory worker（与 deploy/dev/run_all.sh 使用同一入口，不依赖 pip install -e）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

unset VIRTUAL_ENV
exec uv run python -m eidolon.memory.entrypoints.worker "$@"
