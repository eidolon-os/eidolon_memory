"""SQLite/Chroma WAL helpers (D1: simplified — no cache invalidation hacks).

In D1 each palace is owned by exactly one process, so the private-API
``pop_mempalace_client_cache`` / ``close_mempalace_palace`` workarounds that
fought multi-process PersistentClient cache divergence are no longer needed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def ensure_sqlite_wal(
    chroma_sqlite: str,
    *,
    synchronous: str = "FULL",
) -> dict[str, str]:
    """Best-effort WAL mode + synchronous PRAGMA on chroma.sqlite3.

    D3: synchronous=FULL is preferred for chromadb persistence; chroma writes
    are async (NATS-driven) and the 30% commit overhead is acceptable in
    exchange for fsync-per-commit durability.
    """
    p = Path(chroma_sqlite)
    if not p.is_file():
        return {"journal_mode": "missing"}
    sync = (synchronous or "FULL").upper()
    if sync not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
        sync = "FULL"
    conn = sqlite3.connect(str(p), timeout=5.0)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute(f"PRAGMA synchronous={sync}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.commit()
        return {
            "journal_mode": str(mode[0]) if mode else "unknown",
            "synchronous": sync,
        }
    finally:
        conn.close()


def checkpoint_sqlite_wal(chroma_sqlite: str, *, mode: str = "PASSIVE") -> None:
    """Run a WAL checkpoint (PASSIVE by default; TRUNCATE for periodic compaction)."""
    p = Path(chroma_sqlite)
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
