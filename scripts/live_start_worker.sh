#!/usr/bin/env bash
# Start the real JetStream memory worker using the project .venv and YAML config.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

exec .venv/bin/eidolon-memory-worker "$@"
