"""Where each asynchronous command got to, for one memory space.

Statements come from ledger_sql, shared with the shared-storage implementation so
the two cannot diverge on the precedence rules — which status may overwrite which
is the whole correctness content of this ledger.
"""

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
from eidolon.memory.infrastructure.ledger_sql import (
    COMMAND_STATUS_COLUMNS,
    COMMAND_STATUS_COUNT_ALL,
    COMMAND_STATUS_COUNT_BY_STATUS,
    COMMAND_STATUS_INDEX,
    COMMAND_STATUS_INSERT,
    COMMAND_STATUS_OLDEST_ACTIVE,
    COMMAND_STATUS_PRUNE_EXPIRED,
    COMMAND_STATUS_PRUNE_OVERFLOW,
    COMMAND_STATUS_SCHEMA,
    COMMAND_STATUS_SELECT,
    COMMAND_STATUS_UPDATE,
    SQLITE_MARKER,
    ensure_ledger_schema_current,
    render,
)
from eidolon.memory.infrastructure.sqlite_writes import SerialisedSqliteWrites


def _sql(template: str) -> str:
    return render(template, SQLITE_MARKER)


class CommandStatusLedger(SerialisedSqliteWrites):
    """Small SQLite projection queried independently of Chroma/KG locks.

    The command stream remains the write path. This database is only a status
    projection: losing a final status may yield ``accepted`` after restart,
    but can never make an unapplied command look successful.
    """

    def __init__(
        self,
        path: Path,
        *,
        space_id: str,
        retention_days: int = 30,
        max_records: int = 100_000,
        prune_every_writes: int = 100,
    ) -> None:
        if retention_days < 1 or max_records < 1 or prune_every_writes < 1:
            raise ValueError("command status retention limits must be positive")
        self.path = Path(path)
        self._space_id = space_id
        self.retention_days = retention_days
        self.max_records = max_records
        self.prune_every_writes = prune_every_writes
        self._terminal_events: dict[str, asyncio.Event] = {}
        self._terminal_waiters: dict[str, int] = {}
        self._prune_counter_lock = threading.Lock()
        self._writes_since_prune = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_write_lock()
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
            # Before creating: a file from before memory_space_id existed would
            # otherwise open fine and fail on the first statement.
            ensure_ledger_schema_current(
                conn,
                table="command_status",
                required_column="memory_space_id",
                path=self.path,
                # A projection of the command stream, not a record of record. Its
                # documented failure mode is that a lost final status shows a
                # command as accepted again — never that an unapplied one looks
                # successful — so rebuilding costs a diagnostic, not correctness.
                rebuildable=True,
            )
            conn.execute(COMMAND_STATUS_SCHEMA)
            conn.execute(COMMAND_STATUS_INDEX)

    async def record_accepted(self, request_id: str, *, kind: str) -> CommandStatusRecord:
        return await self._write(
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
        return await self._write(
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
        record = await self._write(
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
        record = await self._write(
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
        return await self._read(self._get_sync, request_id)

    async def stats(self) -> CommandStatusStats:
        return await self._read(self._stats_sync)

    async def prune(self) -> int:
        """Remove expired/overflow terminal rows without touching active work."""
        return await self._write(self._prune_sync)

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
                _sql(COMMAND_STATUS_SELECT), (self._space_id, request_id)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def _stats_sync(self) -> CommandStatusStats:
        with self._connect() as conn:
            rows = conn.execute(
                _sql(COMMAND_STATUS_COUNT_BY_STATUS), (self._space_id,)
            ).fetchall()
            oldest = conn.execute(
                _sql(COMMAND_STATUS_OLDEST_ACTIVE), (self._space_id,)
            ).fetchone()[0]
        counts = {str(row[0]): int(row[1]) for row in rows}
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
                _sql(COMMAND_STATUS_SELECT), (self._space_id, request_id)
            ).fetchone()
            if current is not None:
                existing = _from_row(current)
                current_status = existing.status
                # Accepted/retry notifications and late failures never
                # downgrade a terminal outcome. A later successful replay may
                # upgrade failed → applied.
                if current_status == "applied" or (
                    current_status == "failed" and status != "applied"
                ):
                    conn.commit()
                    return existing
                if status == "accepted" and current_status != "accepted":
                    conn.commit()
                    return existing
                attempts = existing.attempts + (1 if status != "accepted" else 0)
                conn.execute(
                    _sql(COMMAND_STATUS_UPDATE),
                    (
                        kind,
                        status,
                        resource_id,
                        error,
                        attempts,
                        now,
                        self._space_id,
                        request_id,
                    ),
                )
            else:
                conn.execute(
                    _sql(COMMAND_STATUS_INSERT),
                    (
                        self._space_id,
                        request_id,
                        kind,
                        status,
                        resource_id,
                        error,
                        0 if status == "accepted" else 1,
                        now,
                        now,
                    ),
                )
            row = conn.execute(
                _sql(COMMAND_STATUS_SELECT), (self._space_id, request_id)
            ).fetchone()
            conn.commit()
        assert row is not None
        self._maybe_prune()
        return _from_row(row)

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
                _sql(COMMAND_STATUS_PRUNE_EXPIRED), (self._space_id, cutoff)
            )
            deleted += max(0, cursor.rowcount)
            total = int(
                conn.execute(
                    _sql(COMMAND_STATUS_COUNT_ALL), (self._space_id,)
                ).fetchone()[0]
            )
            overflow = max(0, total - self.max_records)
            if overflow:
                cursor = conn.execute(
                    _sql(COMMAND_STATUS_PRUNE_OVERFLOW),
                    (self._space_id, self._space_id, overflow),
                )
                deleted += max(0, cursor.rowcount)
        return deleted


def _from_row(row: sqlite3.Row) -> CommandStatusRecord:
    """Read positionally, in the order the shared statements select.

    Not ``SELECT *``: the two dialects return rows differently, and a positional
    read of every column would reorder silently if either changed.
    """

    values = dict(zip(COMMAND_STATUS_COLUMNS, row, strict=True))
    return CommandStatusRecord(
        request_id=str(values["request_id"]),
        kind=str(values["kind"]),
        status=str(values["status"]),  # type: ignore[arg-type]
        resource_id=(
            str(values["resource_id"]) if values["resource_id"] is not None else None
        ),
        error=str(values["error"]) if values["error"] is not None else None,
        attempts=int(values["attempts"]),
        created_at=str(values["created_at"]),
        updated_at=str(values["updated_at"]),
    )
