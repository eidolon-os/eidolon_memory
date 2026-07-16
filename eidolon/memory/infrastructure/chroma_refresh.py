"""SQLite checkpoint helper for Eidolon-owned databases.

Do not apply this helper to Chroma's ``chroma.sqlite3``. Chroma 1.5.9 owns its
journal mode and compaction lifecycle; an external connection changing it to
WAL/checkpointing it caused reproducible SQLITE_IOERR_SHORT_READ (522).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def checkpoint_sqlite_wal(sqlite_path: str, *, mode: str = "PASSIVE") -> None:
    """Run a WAL checkpoint (PASSIVE by default; TRUNCATE for periodic compaction)."""
    p = Path(sqlite_path)
    if not p.is_file():
        return
    cp_mode = (mode or "PASSIVE").upper()
    if cp_mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        cp_mode = "PASSIVE"
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
        try:
            conn.execute(f"PRAGMA wal_checkpoint({cp_mode})")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
