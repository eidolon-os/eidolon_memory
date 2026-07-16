"""Read-optimized status ledger for asynchronous Memory commands."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eidolon.memory.domain.command_status import (
    TERMINAL_COMMAND_STATUSES,
    CommandStatus,
    CommandStatusRecord,
    CommandStatusStats,
)


class CommandStatusLedger:
    """Small SQLite projection queried independently of Chroma/KG locks.

    The command stream remains the write path. This database is only a status
    projection: losing a final status may yield ``accepted`` after restart,
    but can never make an unapplied command look successful.
    """

    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 30,
        max_records: int = 100_000,
        prune_every_writes: int = 100,
    ) -> None:
        if retention_days < 1 or max_records < 1 or prune_every_writes < 1:
            raise ValueError("command status retention limits must be positive")
        self.path = Path(path)
        self.retention_days = retention_days
        self.max_records = max_records
        self.prune_every_writes = prune_every_writes
        self._terminal_events: dict[str, asyncio.Event] = {}
        self._terminal_waiters: dict[str, int] = {}
        self._prune_counter_lock = threading.Lock()
        self._writes_since_prune = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._prune_sync()

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
                CREATE TABLE IF NOT EXISTS command_status (
                    request_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resource_id TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_command_status_updated "
                "ON command_status(updated_at)"
            )

    async def record_accepted(self, request_id: str, *, kind: str) -> CommandStatusRecord:
        return await asyncio.to_thread(
            self._transition,
            request_id,
            kind,
            "accepted",
            None,
            None,
        )

    async def record_retrying(
        self,
        request_id: str,
        *,
        kind: str,
        error: str,
    ) -> CommandStatusRecord:
        return await asyncio.to_thread(
            self._transition,
            request_id,
            kind,
            "retrying",
            None,
            error,
        )

    async def record_applied(
        self,
        request_id: str,
        *,
        kind: str,
        resource_id: str | None = None,
    ) -> CommandStatusRecord:
        record = await asyncio.to_thread(
            self._transition,
            request_id,
            kind,
            "applied",
            resource_id,
            None,
        )
        self._notify_terminal(request_id)
        return record

    async def record_failed(
        self,
        request_id: str,
        *,
        kind: str,
        error: str,
    ) -> CommandStatusRecord:
        record = await asyncio.to_thread(
            self._transition,
            request_id,
            kind,
            "failed",
            None,
            error,
        )
        self._notify_terminal(request_id)
        return record

    async def get(self, request_id: str) -> CommandStatusRecord | None:
        return await asyncio.to_thread(self._get_sync, request_id)

    async def stats(self) -> CommandStatusStats:
        return await asyncio.to_thread(self._stats_sync)

    async def prune(self) -> int:
        """Remove expired/overflow terminal rows without touching active work."""
        return await asyncio.to_thread(self._prune_sync)

    async def wait_terminal(
        self,
        request_id: str,
        *,
        timeout_seconds: float,
    ) -> CommandStatusRecord | None:
        latest = await self.get(request_id)
        timeout = max(0.0, timeout_seconds)
        if latest is not None and latest.status in TERMINAL_COMMAND_STATUSES:
            return latest
        if timeout <= 0:
            return latest

        # MCP and the command worker share one ledger object in agent_runner,
        # so use an in-memory notification instead of opening SQLite every
        # 20ms. Re-read after installing the event to close the completion race.
        event = self._terminal_events.setdefault(request_id, asyncio.Event())
        self._terminal_waiters[request_id] = self._terminal_waiters.get(request_id, 0) + 1
        try:
            latest = await self.get(request_id)
            if latest is not None and latest.status in TERMINAL_COMMAND_STATUSES:
                return latest
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except TimeoutError:
                # Separate-process writers are not part of the production
                # topology, but one final read keeps the projection correct.
                pass
            return await self.get(request_id)
        finally:
            remaining = self._terminal_waiters[request_id] - 1
            if remaining <= 0:
                self._terminal_waiters.pop(request_id, None)
                self._terminal_events.pop(request_id, None)
            else:
                self._terminal_waiters[request_id] = remaining

    def _notify_terminal(self, request_id: str) -> None:
        event = self._terminal_events.get(request_id)
        if event is not None:
            event.set()

    def _get_sync(self, request_id: str) -> CommandStatusRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM command_status WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def _stats_sync(self) -> CommandStatusStats:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM command_status GROUP BY status"
            ).fetchall()
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM command_status "
                "WHERE status IN ('accepted', 'retrying')"
            ).fetchone()[0]
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return CommandStatusStats(
            total=sum(counts.values()),
            accepted=counts.get("accepted", 0),
            retrying=counts.get("retrying", 0),
            applied=counts.get("applied", 0),
            failed=counts.get("failed", 0),
            database_bytes=self.path.stat().st_size if self.path.exists() else 0,
            retention_days=self.retention_days,
            max_records=self.max_records,
            oldest_active_at=str(oldest) if oldest is not None else None,
        )

    def _transition(
        self,
        request_id: str,
        kind: str,
        status: CommandStatus,
        resource_id: str | None,
        error: str | None,
    ) -> CommandStatusRecord:
        request_id = request_id.strip()
        kind = kind.strip()
        if not request_id or not kind:
            raise ValueError("request_id and kind are required")
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM command_status WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if current is not None:
                current_status = str(current["status"])
                # Accepted/retry notifications and late failures never
                # downgrade a terminal outcome. A later successful replay may
                # upgrade failed → applied.
                if current_status == "applied" or (
                    current_status == "failed" and status != "applied"
                ):
                    conn.commit()
                    return self._from_row(current)
                if status == "accepted" and current_status != "accepted":
                    conn.commit()
                    return self._from_row(current)
                attempts = int(current["attempts"]) + (1 if status != "accepted" else 0)
                conn.execute(
                    """
                    UPDATE command_status
                    SET kind = ?, status = ?, resource_id = ?, error = ?,
                        attempts = ?, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (
                        kind,
                        status,
                        resource_id,
                        error,
                        attempts,
                        now,
                        request_id,
                    ),
                )
            else:
                attempts = 0 if status == "accepted" else 1
                conn.execute(
                    """
                    INSERT INTO command_status (
                        request_id, kind, status, resource_id, error,
                        attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        kind,
                        status,
                        resource_id,
                        error,
                        attempts,
                        now,
                        now,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM command_status WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            conn.commit()
        assert row is not None
        self._maybe_prune()
        return self._from_row(row)

    def _maybe_prune(self) -> None:
        should_prune = False
        with self._prune_counter_lock:
            self._writes_since_prune += 1
            if self._writes_since_prune >= self.prune_every_writes:
                self._writes_since_prune = 0
                should_prune = True
        if should_prune:
            self._prune_sync()

    def _prune_sync(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
        deleted = 0
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM command_status
                WHERE status IN ('applied', 'failed') AND updated_at < ?
                """,
                (cutoff,),
            )
            deleted += max(0, cursor.rowcount)
            total = int(conn.execute("SELECT COUNT(*) FROM command_status").fetchone()[0])
            overflow = max(0, total - self.max_records)
            if overflow:
                cursor = conn.execute(
                    """
                    DELETE FROM command_status
                    WHERE request_id IN (
                        SELECT request_id FROM command_status
                        WHERE status IN ('applied', 'failed')
                        ORDER BY updated_at ASC, request_id ASC
                        LIMIT ?
                    )
                    """,
                    (overflow,),
                )
                deleted += max(0, cursor.rowcount)
        return deleted

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CommandStatusRecord:
        return CommandStatusRecord(
            request_id=str(row["request_id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            resource_id=str(row["resource_id"]) if row["resource_id"] is not None else None,
            error=str(row["error"]) if row["error"] is not None else None,
            attempts=int(row["attempts"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
