#!/usr/bin/env bash
# D6: per-user palace snapshot (tar.zst), retained for 6 days × 4 snapshots/day.
#
# Usage (manual):
#   ./scripts/snapshot_palaces.sh
#
# Cron / launchd (every 6 hours):
#   StartCalendarInterval / crontab: 0 */6 * * *
#
# CRITICAL: each palace must wal_checkpoint(TRUNCATE) before tar; otherwise WAL
# entries since the last checkpoint will not be in the snapshot.
set -euo pipefail

PALACES_ROOT="${EIDOLON_MEMORY_PALACES_ROOT:-$HOME/eidolon/palaces}"
SNAP_ROOT="${EIDOLON_MEMORY_SNAPSHOT_ROOT:-$HOME/eidolon/snapshots}"
RETAIN_COUNT="${EIDOLON_MEMORY_SNAPSHOT_RETAIN:-24}"
TS="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$SNAP_ROOT"

if [[ ! -d "$PALACES_ROOT" ]]; then
  echo "[snapshot] no palaces root at $PALACES_ROOT — nothing to do"
  exit 0
fi

shopt -s nullglob
palaces=("$PALACES_ROOT"/*/)
shopt -u nullglob
if (( ${#palaces[@]} == 0 )); then
  echo "[snapshot] no per-user palaces under $PALACES_ROOT — nothing to do"
  exit 0
fi

for palace in "${palaces[@]}"; do
  uid="$(basename "$palace")"
  sqlite="$palace/chroma.sqlite3"
  kg_sqlite="$palace/knowledge_graph.sqlite3"
  if [[ ! -f "$sqlite" ]]; then
    echo "[snapshot] $uid: no chroma.sqlite3, skipping"
    continue
  fi

  echo "[snapshot] $uid: wal_checkpoint(TRUNCATE) on chroma + KG"
  /usr/bin/env python3 -c "
import sqlite3, sys
for path in sys.argv[1:]:
    try:
        conn = sqlite3.connect(path, timeout=10.0)
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as exc:
        # KG file may not yet exist on a fresh palace; treat as benign.
        if 'unable to open database file' not in str(exc):
            raise
" "$sqlite" "$kg_sqlite" || {
    echo "[snapshot][WARN] $uid: wal_checkpoint failed; snapshot may miss recent writes"
  }

  outfile="$SNAP_ROOT/${uid}_${TS}.tar.zst"
  if ! command -v zstd >/dev/null 2>&1; then
    echo "[snapshot][WARN] zstd not on PATH; falling back to plain tar.gz"
    outfile="$SNAP_ROOT/${uid}_${TS}.tar.gz"
    tar -czf "$outfile" -C "$PALACES_ROOT" "$uid"
  else
    tar --use-compress-program=zstd -cf "$outfile" -C "$PALACES_ROOT" "$uid"
  fi
  echo "[snapshot] $uid: -> $outfile"
done

# Retain most recent N per user
shopt -s nullglob
for palace in "${palaces[@]}"; do
  uid="$(basename "$palace")"
  files=("$SNAP_ROOT/${uid}_"*.tar.zst "$SNAP_ROOT/${uid}_"*.tar.gz)
  # sort newest-first
  IFS=$'\n' sorted=($(ls -1t "${files[@]}" 2>/dev/null || true))
  unset IFS
  if (( ${#sorted[@]} > RETAIN_COUNT )); then
    for ((i=RETAIN_COUNT; i<${#sorted[@]}; i++)); do
      rm -v -- "${sorted[$i]}" || true
    done
  fi
done
shopt -u nullglob
