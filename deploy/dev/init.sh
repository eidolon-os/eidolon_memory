#!/usr/bin/env bash
# One-shot dev initialization for eidolon-memory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
CFG="config"
LEGACY="eidolon/memory/config"

mkdir -p "$CFG"

if [ -f "${LEGACY}/memory.default.yaml" ] && [ ! -f "${CFG}/settings.yaml" ]; then
  cp "${LEGACY}/memory.default.yaml" "${CFG}/settings.yaml"
  echo "[INFO] migrated ${LEGACY}/memory.default.yaml -> ${CFG}/settings.yaml"
fi
if [ -f "${LEGACY}/settings.yaml" ] && [ ! -f "${CFG}/settings.yaml" ]; then
  cp "${LEGACY}/settings.yaml" "${CFG}/settings.yaml"
  echo "[INFO] migrated ${LEGACY}/settings.yaml -> ${CFG}/settings.yaml"
fi
if [ ! -f "${CFG}/settings.yaml" ]; then
  cp "${CFG}/settings.example.yaml" "${CFG}/settings.yaml"
  echo "[INFO] created ${CFG}/settings.yaml"
fi
if [ -f "${LEGACY}/.env" ] && [ ! -f "${CFG}/.env" ]; then
  cp "${LEGACY}/.env" "${CFG}/.env"
  echo "[INFO] migrated ${LEGACY}/.env -> ${CFG}/.env"
fi
if [ ! -f "${CFG}/.env" ]; then
  cp "${CFG}/.env.example" "${CFG}/.env"
  echo "[INFO] created ${CFG}/.env — set EIDOLON_MEMORY_LLM_API_KEY"
fi
if [ -f "${LEGACY}/users.yaml" ] && [ ! -f "${CFG}/users.yaml" ]; then
  cp "${LEGACY}/users.yaml" "${CFG}/users.yaml"
  echo "[INFO] migrated ${LEGACY}/users.yaml -> ${CFG}/users.yaml"
fi

mkdir -p "${HOME}/eidolon/run" "${HOME}/eidolon/logs" "${HOME}/eidolon/memory/mempalaces"
echo "[INFO] done."
