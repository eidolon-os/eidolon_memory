#!/usr/bin/env bash
# Local one-shot init for eidolon-memory (D1 per-user palaces).
#
#   ./deploy/dev/init.sh                          # deps + config templates only
#   ./deploy/dev/init.sh --user-id alice          # also init alice's palace + warm
#   ./deploy/dev/init.sh --user-id alice,bob      # batch init multiple users
#   ./deploy/dev/init.sh --all                    # init every enabled user in users.yaml
#   ./deploy/dev/init.sh --skip-sync              # do not run uv sync
#   ./deploy/dev/init.sh --skip-warm              # skip ONNX warm after each init
#   ./deploy/dev/init.sh --with-admin             # also install admin extras
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
[[ -f "$REPO_ROOT/pyproject.toml" ]] || {
  echo "[ERROR] cannot resolve repo root from $0" >&2
  exit 1
}

CFG_DIR="${REPO_ROOT}/eidolon/memory/config"
LOCAL_YAML="${CFG_DIR}/memory.default.yaml"
EXAMPLE_YAML="${CFG_DIR}/memory.default.yaml.example"
LOCAL_USERS="${CFG_DIR}/users.yaml"
EXAMPLE_USERS="${CFG_DIR}/users.yaml.example"

USER_IDS=""        # comma-separated
DO_ALL=0
DO_SYNC=1
DO_WARM=1
WITH_ADMIN=0

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user-id) USER_IDS="$2"; shift ;;
    --user-id=*) USER_IDS="${1#--user-id=}" ;;
    --all) DO_ALL=1 ;;
    --skip-sync) DO_SYNC=0 ;;
    --skip-warm) DO_WARM=0 ;;
    --with-admin) WITH_ADMIN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[ERROR] unknown arg: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

unset VIRTUAL_ENV

command -v uv >/dev/null 2>&1 || {
  error "uv not on PATH (https://docs.astral.sh/uv/)"
  exit 1
}

cd "$REPO_ROOT"

# -------------------- config templates --------------------
if [[ ! -f "$LOCAL_YAML" ]]; then
  if [[ -f "$EXAMPLE_YAML" ]]; then
    cp "$EXAMPLE_YAML" "$LOCAL_YAML"
    info "created local config: ${LOCAL_YAML}"
  else
    warn "missing ${EXAMPLE_YAML}; skipping local config copy"
  fi
else
  info "local config exists: ${LOCAL_YAML}"
fi

if [[ ! -f "$LOCAL_USERS" ]]; then
  if [[ -f "$EXAMPLE_USERS" ]]; then
    cp "$EXAMPLE_USERS" "$LOCAL_USERS"
    info "created local users.yaml: ${LOCAL_USERS}"
  else
    warn "missing ${EXAMPLE_USERS}; users.yaml not seeded"
  fi
fi

# -------------------- deps --------------------
if [[ "$DO_SYNC" == 1 ]]; then
  info "uv sync --extra dev…"
  uv sync --extra dev
  if [[ "$WITH_ADMIN" == 1 ]]; then
    info "uv sync --extra admin…"
    uv sync --extra admin
  fi
fi

# -------------------- user selection --------------------
if [[ "$DO_ALL" == 1 ]]; then
  USER_IDS="$(uv run python -c "
from eidolon.memory.config.users import load_users_config
cfg = load_users_config()
print(','.join(u.id for u in cfg.enabled_users()))
")"
  if [[ -z "$USER_IDS" ]]; then
    warn "users.yaml has no enabled users; nothing to init"
  else
    info "from users.yaml (enabled): ${USER_IDS}"
  fi
fi

if [[ -z "$USER_IDS" ]]; then
  info "no --user-id / --all given; deps+config done. To init a user: $0 --user-id <id>"
  exit 0
fi

# -------------------- NATS JetStream probe (non-fatal) --------------------
info "probing NATS JetStream (nats://127.0.0.1:4222)…"
if uv run python -c "
import asyncio, nats
async def main():
    nc = await nats.connect('nats://127.0.0.1:4222')
    await nc.jetstream().account_info()
    await nc.close()
asyncio.run(main())
" 2>/dev/null; then
  info "NATS JetStream OK"
else
  warn "NATS unreachable or JetStream disabled (writes will queue until it comes up)"
  warn "start with: mkdir -p /tmp/eidolon-nats-js && nats-server -js -p 4222 -sd /tmp/eidolon-nats-js"
fi

# -------------------- per-user init + optional warm --------------------
IFS=',' read -r -a UID_ARR <<< "$USER_IDS"
for raw in "${UID_ARR[@]}"; do
  uid="${raw// /}"  # strip spaces
  [[ -z "$uid" ]] && continue
  info "init user: ${uid}"
  uv run python -c "
import sys
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_for_user
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized

settings = get_memory_settings()
palace = resolve_palace_for_user(settings, sys.argv[1])
print(f'palace: {palace}')
ensure_palace_initialized(sys.argv[1], palace)
" "$uid"

  if [[ "$DO_WARM" == 1 ]]; then
    info "warm runtime for ${uid} (ONNX + closets + sample wing query)…"
    uv run python -c "
import asyncio, sys
from eidolon.memory.application.runtime_warm import warm_palace_read_path
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_for_user

settings = get_memory_settings()
palace = str(resolve_palace_for_user(settings, sys.argv[1]))
asyncio.run(warm_palace_read_path(settings, palace, role='default'))
" "$uid" || warn "warm failed for ${uid} (non-fatal)"
  fi
done

echo ""
info "init done. Next:"
echo "  eidolon-memory-agent --user-id <id>          # single-user dev"
echo "  eidolon-memory-supervisor                    # multi-user (reads users.yaml)"
