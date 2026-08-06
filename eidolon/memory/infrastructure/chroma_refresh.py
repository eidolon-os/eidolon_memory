"""SQLite checkpoint helper for Eidolon-owned databases.

Do not apply this helper to Chroma's ``chroma.sqlite3``. Chroma 1.5.9 owns its
journal mode and compaction lifecycle; an external connection changing it to
WAL/checkpointing it caused reproducible SQLITE_IOERR_SHORT_READ (522).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CheckpointResult:
    """What a checkpoint attempt actually did.

    ``PRAGMA wal_checkpoint`` answers with three numbers and this helper used to
    discard all of them, so the one failure mode that matters here was invisible:
    a checkpoint can return *success* having moved nothing. Any reader holding an
    open snapshot pins the log at that point — the WAL then grows without bound
    while the main file stays frozen, which on an SD card is the worst available
    outcome and, until this returned something, was indistinguishable from a
    healthy idle service.

    ``ran`` is false when there was no file or the attempt raised. It is kept
    distinct from ``busy`` because "no checkpoint happened" and "a checkpoint
    happened and could not proceed" call for different responses.
    """

    ran: bool
    busy: bool = False
    wal_pages: int = 0
    checkpointed_pages: int = 0

    @property
    def stalled(self) -> bool:
        """A log with pages in it that the checkpoint could not move."""

        return self.ran and self.wal_pages > 0 and self.checkpointed_pages == 0


def checkpoint_sqlite_wal(sqlite_path: str, *, mode: str = "PASSIVE") -> CheckpointResult:
    """Run a WAL checkpoint (PASSIVE by default; TRUNCATE for periodic compaction).

    Still best effort — a failed checkpoint costs durability margin, not
    correctness, and must never take down the caller. What changed is that it now
    says so: the exception is no longer swallowed into a bare ``pass`` that made a
    permanently failing checkpoint look exactly like a successful one.
    """

    p = Path(sqlite_path)
    if not p.is_file():
        return CheckpointResult(ran=False)
    cp_mode = (mode or "PASSIVE").upper()
    if cp_mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        cp_mode = "PASSIVE"
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
        try:
            row = conn.execute(f"PRAGMA wal_checkpoint({cp_mode})").fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return CheckpointResult(ran=False)
    if not row:  # pragma: no cover - the pragma always answers on a WAL database
        return CheckpointResult(ran=True)
    # (busy, log_pages, checkpointed_pages). ``-1`` is what SQLite reports for the
    # page counts when the database is not in WAL mode at all, which is not a
    # failure and must not be reported as negative progress.
    busy, wal_pages, checkpointed = (int(value) for value in row[:3])
    return CheckpointResult(
        ran=True,
        busy=busy != 0,
        wal_pages=max(0, wal_pages),
        checkpointed_pages=max(0, checkpointed),
    )
