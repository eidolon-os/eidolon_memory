"""SQLite dead-letter ledger with atomic replay claiming."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from eidolon.memory.domain.dlq import DlqRecord, DlqReplayItem, DlqStats

_STATES = frozenset({"unresolved", "replaying", "replayed", "resolved"})


class DlqLedger:
    """Operational recovery data kept outside Chroma/KG and their lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dlq_entries (
                    entry_id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    error TEXT NOT NULL,
                    deliveries INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    replay_attempts INTEGER NOT NULL DEFAULT 0,
                    resolution_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dlq_state_updated "
                "ON dlq_entries(state, updated_at)"
            )
            conn.execute(
                "UPDATE dlq_entries SET state = 'unresolved' WHERE state = 'replaying'"
            )

    async def add(
        self,
        *,
        subject: str,
        payload: bytes,
        error: str,
        deliveries: int,
    ) -> DlqRecord:
        return await asyncio.to_thread(
            self._add_sync, subject, payload, error, deliveries
        )

    async def get(self, entry_id: str) -> DlqRecord | None:
        return await asyncio.to_thread(self._get_sync, entry_id)

    async def list(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DlqRecord]:
        return await asyncio.to_thread(self._list_sync, state, limit, offset)

    async def claim_replay(self, entry_id: str) -> DlqReplayItem | None:
        return await asyncio.to_thread(self._claim_replay_sync, entry_id)

    async def mark_replayed(self, entry_id: str) -> DlqRecord:
        return await asyncio.to_thread(self._finish_replay_sync, entry_id, True, None)

    async def release_replay(self, entry_id: str, *, error: str) -> DlqRecord:
        return await asyncio.to_thread(self._finish_replay_sync, entry_id, False, error)

    async def resolve(self, entry_id: str, *, note: str) -> DlqRecord:
        return await asyncio.to_thread(self._resolve_sync, entry_id, note)

    async def stats(self) -> DlqStats:
        return await asyncio.to_thread(self._stats_sync)

    def _add_sync(
        self, subject: str, payload: bytes, error: str, deliveries: int
    ) -> DlqRecord:
        if not payload:
            raise ValueError("DLQ payload cannot be empty")
        now = datetime.now(UTC).isoformat()
        entry_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dlq_entries (
                    entry_id, subject, payload, error, deliveries, state,
                    replay_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'unresolved', 0, ?, ?)
                """,
                (entry_id, subject.strip(), payload, error, max(1, deliveries), now, now),
            )
            row = conn.execute(
                "SELECT * FROM dlq_entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        assert row is not None
        return self._from_row(row)

    def _get_sync(self, entry_id: str) -> DlqRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM dlq_entries WHERE entry_id = ?", (entry_id.strip(),)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def _list_sync(self, state: str | None, limit: int, offset: int) -> list[DlqRecord]:
        if state is not None and state not in _STATES:
            raise ValueError("invalid DLQ state")
        lim = max(1, min(limit, 500))
        off = max(0, offset)
        with self._connect() as conn:
            if state is None:
                rows = conn.execute(
                    "SELECT * FROM dlq_entries ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (lim, off),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM dlq_entries WHERE state = ? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (state, lim, off),
                ).fetchall()
        return [self._from_row(row) for row in rows]

    def _claim_replay_sync(self, entry_id: str) -> DlqReplayItem | None:
        clean_id = entry_id.strip()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM dlq_entries WHERE entry_id = ?", (clean_id,)
            ).fetchone()
            if row is None or row["state"] != "unresolved":
                conn.commit()
                return None
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE dlq_entries SET state = 'replaying', "
                "replay_attempts = replay_attempts + 1, "
                "updated_at = ? WHERE entry_id = ?",
                (now, clean_id),
            )
            claimed = conn.execute(
                "SELECT * FROM dlq_entries WHERE entry_id = ?", (clean_id,)
            ).fetchone()
            conn.commit()
        assert claimed is not None
        return DlqReplayItem(record=self._from_row(claimed), payload=bytes(claimed["payload"]))

    def _finish_replay_sync(
        self, entry_id: str, succeeded: bool, error: str | None
    ) -> DlqRecord:
        state = "replayed" if succeeded else "unresolved"
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE dlq_entries SET state = ?, error = COALESCE(?, error), updated_at = ? "
                "WHERE entry_id = ? AND state = 'replaying'",
                (state, error, now, entry_id.strip()),
            )
            if cursor.rowcount != 1:
                raise ValueError("DLQ entry is not claimed for replay")
            row = conn.execute(
                "SELECT * FROM dlq_entries WHERE entry_id = ?", (entry_id.strip(),)
            ).fetchone()
        assert row is not None
        return self._from_row(row)

    def _resolve_sync(self, entry_id: str, note: str) -> DlqRecord:
        clean_note = note.strip()
        if not clean_note:
            raise ValueError("resolution note is required")
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE dlq_entries SET state = 'resolved', resolution_note = ?, updated_at = ? "
                "WHERE entry_id = ? AND state != 'replaying'",
                (clean_note, now, entry_id.strip()),
            )
            if cursor.rowcount != 1:
                raise ValueError("DLQ entry was not found or is replaying")
            row = conn.execute(
                "SELECT * FROM dlq_entries WHERE entry_id = ?", (entry_id.strip(),)
            ).fetchone()
        assert row is not None
        return self._from_row(row)

    def _stats_sync(self) -> DlqStats:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM dlq_entries GROUP BY state"
            ).fetchall()
            payload_bytes = int(
                conn.execute(
                    "SELECT COALESCE(SUM(length(payload)), 0) FROM dlq_entries"
                ).fetchone()[0]
            )
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM dlq_entries WHERE state = 'unresolved'"
            ).fetchone()[0]
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        return DlqStats(
            total=sum(counts.values()),
            unresolved=counts.get("unresolved", 0),
            replaying=counts.get("replaying", 0),
            replayed=counts.get("replayed", 0),
            resolved=counts.get("resolved", 0),
            payload_bytes=payload_bytes,
            database_bytes=self.path.stat().st_size if self.path.exists() else 0,
            oldest_unresolved_at=str(oldest) if oldest is not None else None,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DlqRecord:
        payload = bytes(row["payload"])
        return DlqRecord(
            entry_id=str(row["entry_id"]),
            subject=str(row["subject"]),
            error=str(row["error"]),
            deliveries=int(row["deliveries"]),
            state=str(row["state"]),  # type: ignore[arg-type]
            payload_size=len(payload),
            payload_preview=payload[:500].decode("utf-8", errors="replace"),
            replay_attempts=int(row["replay_attempts"]),
            resolution_note=(
                str(row["resolution_note"]) if row["resolution_note"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
