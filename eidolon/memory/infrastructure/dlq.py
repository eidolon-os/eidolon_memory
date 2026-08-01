"""Turns the service could not process, kept so they can be replayed.

Statements come from ledger_sql, shared with the shared-storage implementation
so the two cannot diverge on what a claim means or which entries a state filter
returns.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from eidolon.memory.domain.dlq import DlqRecord, DlqReplayItem, DlqStats
from eidolon.memory.infrastructure.ledger_sql import (
    DLQ_CLAIM,
    DLQ_COLUMNS,
    DLQ_COUNT_BY_STATE,
    DLQ_ENTRIES_INDEX,
    DLQ_ENTRIES_SCHEMA_TEMPLATE,
    DLQ_FINISH_REPLAY,
    DLQ_INSERT,
    DLQ_OLDEST_UNRESOLVED,
    DLQ_RESOLVE,
    DLQ_SELECT_ONE,
    DLQ_SELECT_PAGE,
    DLQ_SELECT_PAGE_BY_STATE,
    DLQ_STATES,
    SQLITE_MARKER,
    ensure_ledger_schema_current,
    render,
)


def _sql(template: str) -> str:
    return render(template, SQLITE_MARKER)


class DlqLedger:
    """Operational recovery data for one space, outside the palace lock."""

    def __init__(self, path: Path, *, space_id: str) -> None:
        self.path = Path(path)
        self._space_id = space_id
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
            # Before creating: a file from before memory_space_id existed would
            # otherwise open fine and fail on the first statement.
            ensure_ledger_schema_current(
                conn,
                table="dlq_entries",
                required_column="memory_space_id",
                path=self.path,
            )
            conn.execute(DLQ_ENTRIES_SCHEMA_TEMPLATE.format(blob="BLOB"))
            conn.execute(DLQ_ENTRIES_INDEX)
            # A claim can only have been left behind by this process dying,
            # because a palace has exactly one owning process. That reasoning does
            # not hold on shared storage, where the same reset would take an entry
            # away from a live replica — see PostgresDlqLedger.
            conn.execute(
                "UPDATE dlq_entries SET state = 'unresolved' "
                "WHERE memory_space_id = ? AND state = 'replaying'",
                (self._space_id,),
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
                _sql(DLQ_INSERT),
                (
                    self._space_id,
                    entry_id,
                    subject.strip(),
                    payload,
                    error,
                    max(1, deliveries),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                _sql(DLQ_SELECT_ONE), (self._space_id, entry_id)
            ).fetchone()
        assert row is not None
        return _from_row(row)

    def _get_sync(self, entry_id: str) -> DlqRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                _sql(DLQ_SELECT_ONE), (self._space_id, entry_id.strip())
            ).fetchone()
        return _from_row(row) if row is not None else None

    def _list_sync(self, state: str | None, limit: int, offset: int) -> list[DlqRecord]:
        if state is not None and state not in DLQ_STATES:
            raise ValueError("invalid DLQ state")
        lim = max(1, min(limit, 500))
        off = max(0, offset)
        with self._connect() as conn:
            if state is None:
                rows = conn.execute(
                    _sql(DLQ_SELECT_PAGE), (self._space_id, lim, off)
                ).fetchall()
            else:
                rows = conn.execute(
                    _sql(DLQ_SELECT_PAGE_BY_STATE), (self._space_id, state, lim, off)
                ).fetchall()
        return [_from_row(row) for row in rows]

    def _claim_replay_sync(self, entry_id: str) -> DlqReplayItem | None:
        clean_id = entry_id.strip()
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(_sql(DLQ_CLAIM), (now, self._space_id, clean_id))
            if cursor.rowcount != 1:
                conn.commit()
                return None
            claimed = conn.execute(
                _sql(DLQ_SELECT_ONE), (self._space_id, clean_id)
            ).fetchone()
            conn.commit()
        assert claimed is not None
        return DlqReplayItem(
            record=_from_row(claimed), payload=bytes(claimed["payload"])
        )

    def _finish_replay_sync(
        self, entry_id: str, succeeded: bool, error: str | None
    ) -> DlqRecord:
        state = "replayed" if succeeded else "unresolved"
        now = datetime.now(UTC).isoformat()
        clean_id = entry_id.strip()
        with self._connect() as conn:
            cursor = conn.execute(
                _sql(DLQ_FINISH_REPLAY), (state, error, now, self._space_id, clean_id)
            )
            if cursor.rowcount != 1:
                raise ValueError("DLQ entry is not claimed for replay")
            row = conn.execute(
                _sql(DLQ_SELECT_ONE), (self._space_id, clean_id)
            ).fetchone()
        assert row is not None
        return _from_row(row)

    def _resolve_sync(self, entry_id: str, note: str) -> DlqRecord:
        clean_note = note.strip()
        if not clean_note:
            raise ValueError("resolution note is required")
        now = datetime.now(UTC).isoformat()
        clean_id = entry_id.strip()
        with self._connect() as conn:
            cursor = conn.execute(
                _sql(DLQ_RESOLVE), (clean_note, now, self._space_id, clean_id)
            )
            if cursor.rowcount != 1:
                raise ValueError("DLQ entry was not found or is replaying")
            row = conn.execute(
                _sql(DLQ_SELECT_ONE), (self._space_id, clean_id)
            ).fetchone()
        assert row is not None
        return _from_row(row)

    def _stats_sync(self) -> DlqStats:
        with self._connect() as conn:
            rows = conn.execute(
                _sql(DLQ_COUNT_BY_STATE), (self._space_id,)
            ).fetchall()
            payload_bytes = int(
                conn.execute(
                    "SELECT COALESCE(SUM(length(payload)), 0) FROM dlq_entries "
                    "WHERE memory_space_id = ?",
                    (self._space_id,),
                ).fetchone()[0]
            )
            oldest = conn.execute(
                _sql(DLQ_OLDEST_UNRESOLVED), (self._space_id,)
            ).fetchone()[0]
        counts = {str(row[0]): int(row[1]) for row in rows}
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


def _from_row(row: sqlite3.Row) -> DlqRecord:
    values = dict(zip(DLQ_COLUMNS, row, strict=True))
    payload = bytes(values["payload"])
    return DlqRecord(
        entry_id=str(values["entry_id"]),
        subject=str(values["subject"]),
        error=str(values["error"]),
        deliveries=int(values["deliveries"]),
        state=str(values["state"]),  # type: ignore[arg-type]
        payload_size=len(payload),
        payload_preview=payload[:500].decode("utf-8", errors="replace"),
        replay_attempts=int(values["replay_attempts"]),
        resolution_note=(
            str(values["resolution_note"])
            if values["resolution_note"] is not None
            else None
        ),
        created_at=str(values["created_at"]),
        updated_at=str(values["updated_at"]),
    )
