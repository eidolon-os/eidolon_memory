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

# Stage a consistent copy of one space, then archive the staging directory.
#
# Every SQLite file goes through `VACUUM INTO`, which runs inside a read
# transaction and therefore produces a point-in-time copy of a database that is
# being written to — no checkpoint, no writer lock, no torn pages. The previous
# version checkpointed and then tarred the *live* file; tar reads pages over
# time, so a writer active during that read yields an archive whose
# chroma.sqlite3 is torn. That is real data loss, because chroma.sqlite3 is the
# authoritative copy: `mempalace repair --mode from-sqlite` rebuilds a whole
# palace from it, re-embedding the drawer text it holds.
#
# Which is also why the HNSW segment files are copied best-effort and not
# guarded. They hold vectors only, and vectors are re-derivable — the embedding
# model is deterministic, so the same text yields the same vector. A stale or
# torn .bin costs a rebuild, not a memory.
stage_space() {
  local palace="$1" ledgers="$2" stage="$3" uid="$4"
  /usr/bin/env python3 - "$palace" "$ledgers" "$stage" "$uid" <<'PY'
import shutil
import sqlite3
import sys
from pathlib import Path

palace, ledgers, stage, uid = (Path(sys.argv[1]), Path(sys.argv[2]),
                               Path(sys.argv[3]), sys.argv[4])


def snapshot_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Read-only, so a snapshot can never create or migrate a database. Opening
    # read-write is how an earlier version manufactured an empty graph inside
    # the palace and then archived it.
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
    try:
        conn.execute("VACUUM INTO ?", (str(dst),))
    finally:
        conn.close()


def copy_plain(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


copied = vacuumed = 0
for src in sorted(palace.rglob("*")):
    if not src.is_file():
        continue
    rel = src.relative_to(palace)
    # -wal and -shm belong to the live database and are meaningless beside a
    # vacuumed copy, which has neither.
    if src.suffix in (".sqlite3-wal", ".sqlite3-shm") or src.name.endswith(("-wal", "-shm")):
        continue
    dst = stage / uid / rel
    if src.suffix == ".sqlite3":
        snapshot_db(src, dst)
        vacuumed += 1
    else:
        copy_plain(src, dst)
        copied += 1

if ledgers.is_dir():
    for src in sorted(ledgers.glob("*.sqlite3")):
        snapshot_db(src, stage / f"{uid}.ledgers" / src.name)
        vacuumed += 1

print(f"{vacuumed} {copied}")
PY
}

for palace in "${palaces[@]}"; do
  uid="$(basename "$palace")"
  ledgers="$palace.ledgers"
  stage="$SNAP_ROOT/.staging/${uid}_${TS}"

  if [[ ! -d "$ledgers" ]]; then
    echo "[snapshot][WARN] $uid: no $uid.ledgers beside the palace — graph and ledgers not in this snapshot"
  fi

  rm -rf "$stage"
  mkdir -p "$stage"
  if ! counts="$(stage_space "$palace" "$ledgers" "$stage" "$uid")"; then
    echo "[snapshot][FAIL] $uid: could not stage a consistent copy; skipping"
    rm -rf "$stage"
    continue
  fi
  echo "[snapshot] $uid: staged ${counts% *} database(s) via VACUUM INTO, ${counts#* } other file(s)"

  # Named members rather than ".", so the archive holds `<uid>/…` exactly as the
  # palaces root does and a restore is `tar -xf` in place. COPYFILE_DISABLE stops
  # BSD tar from storing macOS xattrs as sibling ._ entries, which would restore
  # as junk files inside the palace.
  members=("$uid")
  [[ -d "$stage/$uid.ledgers" ]] && members+=("$uid.ledgers")

  outfile="$SNAP_ROOT/${uid}_${TS}.tar.zst"
  if ! command -v zstd >/dev/null 2>&1; then
    echo "[snapshot][WARN] zstd not on PATH; falling back to plain tar.gz"
    outfile="$SNAP_ROOT/${uid}_${TS}.tar.gz"
    COPYFILE_DISABLE=1 tar -czf "$outfile" -C "$stage" "${members[@]}"
  else
    COPYFILE_DISABLE=1 tar --use-compress-program=zstd -cf "$outfile" -C "$stage" "${members[@]}"
  fi
  rm -rf "$stage"
  echo "[snapshot] $uid: -> $outfile"
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
