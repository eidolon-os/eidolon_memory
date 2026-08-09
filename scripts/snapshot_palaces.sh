#!/usr/bin/env bash
# Per-user snapshot of everything a space owns (tar.zst), N retained per user.
#
# Usage (manual):
#   ./scripts/snapshot_palaces.sh
#
# Cron / launchd (every 6 hours):
#   StartCalendarInterval / crontab: 0 */6 * * *
#
# A space is TWO directories, and both are data:
#
#   <palaces_root>/<uid>/           mempalace's: chroma.sqlite3 + the HNSW index
#   <palaces_root>/<uid>.ledgers/   ours: 7 SQLite files, one of them the graph
#
# It was one directory when this script was written. Moving the ledgers out (so
# `mempalace repair`'s os.rename of the palace could not take them with it) broke
# the snapshot silently and in three ways at once, all of them found by actually
# looking inside a tarball rather than at the script:
#
#   1. `<uid>.ledgers/` matched the `*/` glob, was tested for a chroma.sqlite3 it
#      does not have, and was skipped as "not a palace". Nothing archived it, so
#      the graph, the canonical facts and the commitments were absent from every
#      snapshot taken after the move.
#   2. The checkpoint step still pointed at `<palace>/knowledge_graph.sqlite3`.
#      `sqlite3.connect` CREATES a missing file, so each run manufactured an empty
#      database in the palace — and put it in the tarball, where a restore would
#      lay down a plausible-looking zero-row graph.
#   3. The retention loop expands `files=(...zst ...gz)` under `set -u`; with no
#      .gz ever produced the array is unset and the script dies before pruning.
#      It had already written the snapshot, so this read as success from cron.
#
# CRITICAL: every database is wal_checkpoint(TRUNCATE)ed before tar, or writes
# since the last checkpoint sit in a -wal the archive does not have.
set -euo pipefail

EIDOLON_MEMORY_STATE_ROOT="${EIDOLON_STATE_ROOT:-$HOME/eidolon/data}/memory"
PALACES_ROOT="${EIDOLON_MEMORY_PALACES_ROOT:-$EIDOLON_MEMORY_STATE_ROOT/mempalaces}"
SNAP_ROOT="${EIDOLON_MEMORY_SNAPSHOT_ROOT:-$EIDOLON_MEMORY_STATE_ROOT/snapshots}"
RETAIN_COUNT="${EIDOLON_MEMORY_SNAPSHOT_RETAIN:-24}"
TS="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$SNAP_ROOT"

if [[ ! -d "$PALACES_ROOT" ]]; then
  echo "[snapshot] no palaces root at $PALACES_ROOT — nothing to do"
  exit 0
fi

shopt -s nullglob
candidates=("$PALACES_ROOT"/*/)
shopt -u nullglob

# A palace is a directory holding a chroma.sqlite3. That test also excludes the
# sibling `.ledgers` directory, which is archived with its palace rather than as
# one of its own.
palaces=()
for dir in "${candidates[@]}"; do
  [[ -f "${dir%/}/chroma.sqlite3" ]] && palaces+=("${dir%/}")
done

if (( ${#palaces[@]} == 0 )); then
  echo "[snapshot] no per-user palaces under $PALACES_ROOT — nothing to do"
  exit 0
fi

checkpoint() {
  # Never creates. A path that is not already a database is not one this script
  # should bring into being — that is how an empty graph got into a backup.
  /usr/bin/env python3 -c '
import sqlite3, sys
from pathlib import Path

for path in sys.argv[1:]:
    if not Path(path).is_file():
        continue
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
' "$@"
}

for palace in "${palaces[@]}"; do
  uid="$(basename "$palace")"
  ledgers="$palace.ledgers"

  shopt -s nullglob
  databases=("$palace/chroma.sqlite3" "$ledgers"/*.sqlite3)
  shopt -u nullglob

  echo "[snapshot] $uid: wal_checkpoint(TRUNCATE) on ${#databases[@]} database(s)"
  checkpoint "${databases[@]}" || {
    echo "[snapshot][WARN] $uid: wal_checkpoint failed; snapshot may miss recent writes"
  }

  # Both directories, named relative to the root so a restore is a plain
  # extract in place. The ledgers directory is absent on a palace that has
  # never been written to, which is not an error.
  members=("$uid")
  [[ -d "$ledgers" ]] && members+=("$uid.ledgers")
  if (( ${#members[@]} == 1 )); then
    echo "[snapshot][WARN] $uid: no $uid.ledgers beside the palace — graph and ledgers not in this snapshot"
  fi

  outfile="$SNAP_ROOT/${uid}_${TS}.tar.zst"
  if ! command -v zstd >/dev/null 2>&1; then
    echo "[snapshot][WARN] zstd not on PATH; falling back to plain tar.gz"
    outfile="$SNAP_ROOT/${uid}_${TS}.tar.gz"
    tar -czf "$outfile" -C "$PALACES_ROOT" "${members[@]}"
  else
    tar --use-compress-program=zstd -cf "$outfile" -C "$PALACES_ROOT" "${members[@]}"
  fi
  echo "[snapshot] $uid: -> $outfile (${#members[@]} director$([[ ${#members[@]} == 1 ]] && echo y || echo ies))"
done

# Retain the most recent N per user. nullglob is what keeps an unmatched pattern
# from surviving as a literal, and the explicit empty check is what keeps `set -u`
# from killing the run after the snapshots were already written.
shopt -s nullglob
for palace in "${palaces[@]}"; do
  uid="$(basename "$palace")"
  files=("$SNAP_ROOT/${uid}_"*.tar.zst "$SNAP_ROOT/${uid}_"*.tar.gz)
  (( ${#files[@]} > RETAIN_COUNT )) || continue
  IFS=$'\n' read -r -d '' -a sorted < <(ls -1t "${files[@]}" && printf '\0')
  unset IFS
  for ((i=RETAIN_COUNT; i<${#sorted[@]}; i++)); do
    rm -v -- "${sorted[$i]}" || true
  done
done
shopt -u nullglob
