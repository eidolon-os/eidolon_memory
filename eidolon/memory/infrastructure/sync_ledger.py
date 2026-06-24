"""SQLite idempotency ledger for offline device sync batches."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class SyncLedger:
    """Tracks synced device outbox events for one memory-space palace."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path))

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_events (
                    event_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    idempotency_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_idempotency "
                "ON sync_events(idempotency_hash)"
            )

    def seen(self, *, event_id: str, idempotency_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sync_events WHERE event_id = ? OR idempotency_hash = ?",
                (event_id, idempotency_hash),
            ).fetchone()
            return row is not None

    def mark_synced(
        self,
        *,
        event_id: str,
        device_id: str,
        instance_id: str,
        turn_id: str,
        idempotency_hash: str,
    ) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sync_events (
                    event_id, device_id, instance_id, turn_id,
                    idempotency_hash, status, synced_at
                ) VALUES (?, ?, ?, ?, ?, 'synced', ?)
                """,
                (event_id, device_id, instance_id, turn_id, idempotency_hash, now),
            )
