"""Which offline sync batches have already been applied, for one memory space.

A device that was offline replays its outbox when it reconnects, and a replay
that is not recognised writes the same turns into memory again. This is the
record that makes the replay idempotent.

Statements come from ledger_sql, shared with the shared-storage implementation so
the two cannot diverge on what counts as already-applied.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from eidolon.memory.infrastructure.sqlite_writes import SerialisedSqliteWrites
from eidolon.memory.infrastructure.ledger_sql import (
    SQLITE_MARKER,
    SYNC_EVENT_INSERT,
    SYNC_EVENT_SEEN,
    SYNC_EVENTS_INDEX,
    SYNC_EVENTS_SCHEMA,
    ensure_ledger_schema_current,
    render,
)


class SyncLedger(SerialisedSqliteWrites):
    """Sync idempotency for one space, in a file inside its palace."""

    def __init__(self, path: str | Path, *, space_id: str) -> None:
        self._path = Path(path)
        self._space_id = space_id
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_write_lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=5.0)
        # Matches the other five ledgers. Reachable despite the write lock: a
        # process that crashed mid-write can leave the file locked briefly, and
        # without this the next connection fails instead of waiting.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            # Before creating: a file from before memory_space_id existed would
            # otherwise open fine and fail on the first statement.
            ensure_ledger_schema_current(
                conn,
                table="sync_events",
                required_column="memory_space_id",
                path=self._path,
            )
            conn.execute(SYNC_EVENTS_SCHEMA)
            conn.execute(SYNC_EVENTS_INDEX)

    async def seen(self, *, event_id: str, idempotency_hash: str) -> bool:
        """Whether this event or an identical payload was already applied."""
        return await self._read(self._seen_sync, event_id, idempotency_hash)

    async def mark_synced(
        self,
        *,
        event_id: str,
        device_id: str,
        instance_id: str,
        turn_id: str,
        idempotency_hash: str,
    ) -> None:
        """Record that this event was applied. Idempotent."""
        await self._write(
            self._mark_synced_sync,
            event_id,
            device_id,
            instance_id,
            turn_id,
            idempotency_hash,
        )

    def _seen_sync(self, event_id: str, idempotency_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                render(SYNC_EVENT_SEEN, SQLITE_MARKER),
                (self._space_id, event_id, idempotency_hash),
            ).fetchone()
            return row is not None

    def _mark_synced_sync(
        self,
        event_id: str,
        device_id: str,
        instance_id: str,
        turn_id: str,
        idempotency_hash: str,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            try:
                conn.execute(
                    render(SYNC_EVENT_INSERT, SQLITE_MARKER),
                    (
                        self._space_id,
                        event_id,
                        device_id,
                        instance_id,
                        turn_id,
                        idempotency_hash,
                        "synced",
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Already applied. Reached by a caller that skipped ``seen`` or
                # raced one, and either way the record it wanted exists — which
                # is the outcome, not an error.
                pass
