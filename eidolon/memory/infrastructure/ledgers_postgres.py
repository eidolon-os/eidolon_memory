"""The ledgers as rows in a shared database, for deployments with replicas.

Same design as the SQLite ledgers next door — the shapes and statements come from
ledger_sql, so neither dialect can drift from the other unnoticed. What changes
here is the mechanics: a connection pool instead of a file, PostgreSQL's marker,
and server-side transactions instead of ``BEGIN IMMEDIATE``.

Why a pool and not a connection per ledger: a replica serves every space, so
per-space connections would grow with tenants rather than with concurrency.

No locks. The embedded ledgers rely on a single owning process; here several
replicas write concurrently and correctness comes from the primary keys and
transactions, which is what lets replicas scale horizontally at all. A lock held
across a network round trip would serialise exactly the requests this deployment
exists to run in parallel.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from eidolon_memory_contracts import MemoryIntent

from eidolon.memory.domain.command_status import (
    TERMINAL_COMMAND_STATUSES,
    CommandStatus,
    CommandStatusRecord,
    CommandStatusStats,
)
from eidolon.memory.domain.commitment import (
    ACTIVE_COMMITMENT_STATUSES,
    CommitmentApplyResult,
    CommitmentConflict,
    CommitmentListPage,
    CommitmentRecord,
    CommitmentRevisionRecord,
    commitment_identity,
)
from eidolon.memory.domain.commitment_decision import decide_commitment_apply
from eidolon.memory.domain.dlq import DlqRecord, DlqReplayItem, DlqStats
from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
)
from eidolon.memory.domain.steward import StewardDecision

# The pure helpers the decision needs. Imported from the embedded ledger because
# they are properties of commitments, not of SQLite — the alternative is a third
# module holding four small functions.
from eidolon.memory.infrastructure.commitments import (
    _intent_fields,
    _intent_hash,
    _merge_values,
    _requested_status,
    _validate_identity,
)
from eidolon.memory.infrastructure.commitments import (
    _record_values as _commitment_values,
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
    COMMITMENT_COLUMNS,
    COMMITMENT_COUNT,
    COMMITMENT_INSERT,
    COMMITMENT_REVISION_BY_ID,
    COMMITMENT_REVISION_BY_INTENT,
    COMMITMENT_REVISION_COLUMNS,
    COMMITMENT_REVISION_HISTORY,
    COMMITMENT_REVISION_INSERT,
    COMMITMENT_REVISIONS_INDEX,
    COMMITMENT_REVISIONS_SCHEMA,
    COMMITMENT_SELECT_BY_ID,
    COMMITMENT_SELECT_ONE,
    COMMITMENT_SELECT_PAGE,
    COMMITMENT_UPDATE,
    COMMITMENTS_INDEX,
    COMMITMENTS_SCHEMA,
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
    EXTRACTION_DECISION_COLUMNS,
    EXTRACTION_DECISION_INSERT,
    EXTRACTION_DECISION_SELECT,
    EXTRACTION_DECISIONS_SCHEMA,
    POSTGRES_MARKER,
    SYNC_EVENT_INSERT,
    SYNC_EVENT_SEEN,
    SYNC_EVENTS_INDEX,
    SYNC_EVENTS_SCHEMA,
    commitment_count_active,
    commitment_mark_projected,
    commitment_select_active_page,
    render,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def require_pool_driver() -> Any:
    """The pool class, or an error naming the extra that provides it.

    Shared by every ledger here so an operator who set shared storage without
    the driver gets the same actionable message wherever they first hit it.
    """

    try:
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise RuntimeError(
            "shared-storage ledgers need the 'postgres' extra "
            "(pip install 'eidolon-memory[postgres]')"
        ) from exc
    return AsyncConnectionPool


class PostgresExtractionDecisionLedger:
    """Validated steward output, shared across replicas.

    Cloud needs this more than local does, not less: without it two replicas
    processing the same turn would each call the model and could reach different
    conclusions, so the record of what was decided is what makes re-processing
    idempotent rather than merely repeated.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 8):
        pool_cls = require_pool_driver()
        pool = pool_cls(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        ledger = cls(pool)
        await ledger.ensure_schema()
        return ledger

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(EXTRACTION_DECISIONS_SCHEMA)

    async def get(
        self,
        memory_space_id: str,
        source_turn_id: str,
        extractor_version: str,
    ) -> ExtractionDecisionRecord | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(EXTRACTION_DECISION_SELECT, POSTGRES_MARKER),
                (memory_space_id, source_turn_id, extractor_version),
            )
            row = await cursor.fetchone()
        return _decision_from_row(row) if row is not None else None

    async def put_if_absent(
        self,
        record: ExtractionDecisionRecord,
    ) -> ExtractionDecisionRecord:
        """Store this decision, or return the one already stored for its identity.

        The read and the insert share one transaction. Two replicas processing the
        same turn will have one of them block on the row and then see the other's
        decision, rather than both inserting and one failing on the primary key —
        which would surface as an error for work that actually succeeded.
        """

        async with self._pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    render(EXTRACTION_DECISION_SELECT, POSTGRES_MARKER)
                    # Holds the row against a concurrent writer for the length of
                    # the transaction. Without it both replicas read absent and
                    # both insert.
                    + " FOR UPDATE",
                    (
                        record.memory_space_id,
                        record.source_turn_id,
                        record.extractor_version,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await conn.execute(
                        render(EXTRACTION_DECISION_INSERT, POSTGRES_MARKER),
                        _decision_to_row(record),
                    )
                    return record

        stored = _decision_from_row(row)
        if stored.input_hash != record.input_hash:
            raise ExtractionDecisionConflict(
                "extraction identity reused with different validated turn input"
            )
        return stored


class PostgresSyncLedger:
    """Sync idempotency for one space, in a table every replica shares.

    Scoped by ``space_id`` on every statement. The embedded ledger gets isolation
    from living inside one palace; here the column is the only thing separating
    one owner's sync history from another's.
    """

    def __init__(self, pool: Any, *, space_id: str) -> None:
        self._pool = pool
        self._space_id = space_id

    @classmethod
    async def connect(cls, dsn: str, *, space_id: str, min_size: int = 1, max_size: int = 8):
        pool_cls = require_pool_driver()
        pool = pool_cls(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        ledger = cls(pool, space_id=space_id)
        await ledger.ensure_schema()
        return ledger

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SYNC_EVENTS_SCHEMA)
            await conn.execute(SYNC_EVENTS_INDEX)

    async def seen(self, *, event_id: str, idempotency_hash: str) -> bool:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(SYNC_EVENT_SEEN, POSTGRES_MARKER),
                (self._space_id, event_id, idempotency_hash),
            )
            return await cursor.fetchone() is not None

    async def mark_synced(
        self,
        *,
        event_id: str,
        device_id: str,
        instance_id: str,
        turn_id: str,
        idempotency_hash: str,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with self._pool.connection() as conn:
            await conn.execute(
                render(SYNC_EVENT_INSERT, POSTGRES_MARKER) + " ON CONFLICT DO NOTHING",
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


class PostgresDlqLedger:
    """Failed turns for one space, in a table every replica shares.

    One behaviour is deliberately absent. The embedded ledger resets every
    ``replaying`` entry to ``unresolved`` when it opens, on the reasoning that a
    claim can only have been left behind by this process crashing. With replicas
    that reasoning is wrong and the reset is destructive: a starting replica
    would hand another replica's in-flight entry to a second worker.

    A stale claim is instead released explicitly by
    :meth:`release_stale_claims`, which an operator or a periodic task runs with
    an age threshold. Slower to recover, and it cannot duplicate work.
    """

    def __init__(self, pool: Any, *, space_id: str) -> None:
        self._pool = pool
        self._space_id = space_id

    @classmethod
    async def connect(cls, dsn: str, *, space_id: str, min_size: int = 1, max_size: int = 8):
        pool_cls = require_pool_driver()
        pool = pool_cls(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        ledger = cls(pool, space_id=space_id)
        await ledger.ensure_schema()
        return ledger

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(DLQ_ENTRIES_SCHEMA_TEMPLATE.format(blob="BYTEA"))
            await conn.execute(DLQ_ENTRIES_INDEX)

    async def add(
        self,
        *,
        subject: str,
        payload: bytes,
        error: str,
        deliveries: int,
    ) -> DlqRecord:
        if not payload:
            raise ValueError("DLQ payload cannot be empty")
        now = datetime.now(UTC).isoformat()
        entry_id = uuid.uuid4().hex
        async with self._pool.connection() as conn:
            await conn.execute(
                render(DLQ_INSERT, POSTGRES_MARKER),
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
        record = await self.get(entry_id)
        assert record is not None
        return record

    async def get(self, entry_id: str) -> DlqRecord | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(DLQ_SELECT_ONE, POSTGRES_MARKER),
                (self._space_id, entry_id.strip()),
            )
            row = await cursor.fetchone()
        return _dlq_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DlqRecord]:
        if state is not None and state not in DLQ_STATES:
            raise ValueError("invalid DLQ state")
        lim = max(1, min(limit, 500))
        off = max(0, offset)
        async with self._pool.connection() as conn:
            if state is None:
                cursor = await conn.execute(
                    render(DLQ_SELECT_PAGE, POSTGRES_MARKER),
                    (self._space_id, lim, off),
                )
            else:
                cursor = await conn.execute(
                    render(DLQ_SELECT_PAGE_BY_STATE, POSTGRES_MARKER),
                    (self._space_id, state, lim, off),
                )
            rows = await cursor.fetchall()
        return [_dlq_from_row(row) for row in rows]

    async def claim_replay(self, entry_id: str) -> DlqReplayItem | None:
        """Take exclusive ownership of an entry, or None if someone else has it.

        The conditional UPDATE is the claim. Two replicas racing produce one
        row updated and one row not — no lock, and no window where both believe
        they own it.

        Stale claims are released first, here rather than on a schedule. This is
        the only moment anything cares whether an abandoned claim exists, so a
        periodic task would either run when nobody was asking or leave an entry
        stuck until it happened to fire. Without this the recovery method had no
        caller at all and an entry left by a dead replica stayed unreplayable.
        """

        released = await self.release_stale_claims()
        if released:
            log.info("dlq_stale_claims_released", memory_space_id=self._space_id, count=released)

        clean_id = entry_id.strip()
        now = datetime.now(UTC).isoformat()
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(DLQ_CLAIM, POSTGRES_MARKER),
                (now, self._space_id, clean_id),
            )
            if cursor.rowcount != 1:
                return None
        record = await self.get(clean_id)
        assert record is not None
        payload = await self._payload_of(clean_id)
        return DlqReplayItem(record=record, payload=payload)

    async def mark_replayed(self, entry_id: str) -> DlqRecord:
        return await self._finish_replay(entry_id, succeeded=True, error=None)

    async def release_replay(self, entry_id: str, *, error: str) -> DlqRecord:
        return await self._finish_replay(entry_id, succeeded=False, error=error)

    async def resolve(self, entry_id: str, *, note: str) -> DlqRecord:
        clean_note = note.strip()
        if not clean_note:
            raise ValueError("resolution note is required")
        now = datetime.now(UTC).isoformat()
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(DLQ_RESOLVE, POSTGRES_MARKER),
                (clean_note, now, self._space_id, entry_id.strip()),
            )
            if cursor.rowcount != 1:
                raise ValueError("DLQ entry was not found or is replaying")
        record = await self.get(entry_id)
        assert record is not None
        return record

    # Must exceed the longest replay a healthy worker performs, because age is
    # the only signal available: a replica cannot tell a dead peer from a busy
    # one. Fifteen minutes is far above any observed replay and far below the
    # point where a stuck entry matters operationally.
    STALE_CLAIM_SECONDS = 900

    async def release_stale_claims(self, *, older_than_seconds: int | None = None) -> int:
        """Hand back claims whose worker never finished, and say how many.

        Replaces the embedded ledger's reset-on-open, which would be destructive
        here — a starting replica cannot distinguish another replica's in-flight
        entry from an abandoned one.

        Called from :meth:`claim_replay`; the argument exists so an operator can
        force a shorter threshold when they know a replica is gone.
        """

        if older_than_seconds is None:
            older_than_seconds = self.STALE_CLAIM_SECONDS

        cutoff = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - older_than_seconds, tz=UTC
        ).isoformat()
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE dlq_entries SET state = 'unresolved', updated_at = %s "
                "WHERE memory_space_id = %s AND state = 'replaying' AND updated_at < %s",
                (datetime.now(UTC).isoformat(), self._space_id, cutoff),
            )
            return cursor.rowcount

    async def stats(self) -> DlqStats:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(DLQ_COUNT_BY_STATE, POSTGRES_MARKER), (self._space_id,)
            )
            counts = {str(state): int(count) for state, count in await cursor.fetchall()}

            cursor = await conn.execute(
                "SELECT COALESCE(SUM(octet_length(payload)), 0) FROM dlq_entries "
                "WHERE memory_space_id = %s",
                (self._space_id,),
            )
            payload_bytes = int((await cursor.fetchone())[0])

            cursor = await conn.execute(
                render(DLQ_OLDEST_UNRESOLVED, POSTGRES_MARKER), (self._space_id,)
            )
            oldest = (await cursor.fetchone())[0]

        return DlqStats(
            total=sum(counts.values()),
            unresolved=counts.get("unresolved", 0),
            replaying=counts.get("replaying", 0),
            replayed=counts.get("replayed", 0),
            resolved=counts.get("resolved", 0),
            payload_bytes=payload_bytes,
            # A shared table has no file, and reporting the whole table's size
            # would attribute every space's rows to whichever one asked.
            database_bytes=0,
            oldest_unresolved_at=str(oldest) if oldest is not None else None,
        )

    async def _finish_replay(
        self, entry_id: str, *, succeeded: bool, error: str | None
    ) -> DlqRecord:
        state = "replayed" if succeeded else "unresolved"
        now = datetime.now(UTC).isoformat()
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(DLQ_FINISH_REPLAY, POSTGRES_MARKER),
                (state, error, now, self._space_id, entry_id.strip()),
            )
            if cursor.rowcount != 1:
                raise ValueError("DLQ entry is not claimed for replay")
        record = await self.get(entry_id)
        assert record is not None
        return record

    async def _payload_of(self, entry_id: str) -> bytes:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT payload FROM dlq_entries "
                "WHERE memory_space_id = %s AND entry_id = %s",
                (self._space_id, entry_id),
            )
            row = await cursor.fetchone()
        return bytes(row[0]) if row is not None else b""


class PostgresCommandStatusLedger:
    """Where each asynchronous command got to, visible from every replica.

    One behaviour had to change shape. The embedded ledger wakes a waiter with an
    in-process ``asyncio.Event``, which is exact and free because MCP and the
    command worker share one object. With replicas they do not: a request served
    by one replica may be waiting on a command applied by another, and an event
    set over there is never seen over here.

    So ``wait_terminal`` polls. That is slower and it is the only thing that
    works — the alternative, LISTEN/NOTIFY, needs a dedicated connection held
    open per waiter, which is a worse trade at this size.
    """

    # Fast enough that a caller does not perceive it against command latency,
    # slow enough that a handful of concurrent waiters is not a load source.
    POLL_INTERVAL_SECONDS = 0.05

    def __init__(
        self,
        pool: Any,
        *,
        space_id: str,
        retention_days: int = 30,
        max_records: int = 100_000,
        prune_every_writes: int = 100,
    ) -> None:
        if retention_days < 1 or max_records < 1 or prune_every_writes < 1:
            raise ValueError("command status retention limits must be positive")
        self._pool = pool
        self._space_id = space_id
        self.retention_days = retention_days
        self.max_records = max_records
        self.prune_every_writes = prune_every_writes
        self._writes_since_prune = 0

    @classmethod
    async def connect(cls, dsn: str, *, space_id: str, min_size: int = 1, max_size: int = 8):
        pool_cls = require_pool_driver()
        pool = pool_cls(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        ledger = cls(pool, space_id=space_id)
        await ledger.ensure_schema()
        return ledger

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(COMMAND_STATUS_SCHEMA)
            await conn.execute(COMMAND_STATUS_INDEX)

    async def record_accepted(self, request_id: str, *, kind: str) -> CommandStatusRecord:
        return await self._transition(request_id, kind, "accepted", None, None)

    async def record_retrying(
        self, request_id: str, *, kind: str, error: str
    ) -> CommandStatusRecord:
        return await self._transition(request_id, kind, "retrying", None, error)

    async def record_applied(
        self, request_id: str, *, kind: str, resource_id: str | None = None
    ) -> CommandStatusRecord:
        return await self._transition(request_id, kind, "applied", resource_id, None)

    async def record_failed(
        self, request_id: str, *, kind: str, error: str
    ) -> CommandStatusRecord:
        return await self._transition(request_id, kind, "failed", None, error)

    async def get(self, request_id: str) -> CommandStatusRecord | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(COMMAND_STATUS_SELECT, POSTGRES_MARKER),
                (self._space_id, request_id),
            )
            row = await cursor.fetchone()
        return _command_status_from_row(row) if row is not None else None

    async def wait_terminal(
        self, request_id: str, *, timeout_seconds: float
    ) -> CommandStatusRecord | None:
        """Block until the command reaches a terminal status, or the budget runs out.

        Returns whatever the latest known status is on timeout rather than
        raising: the caller asked how far a command got, and "still running" is an
        answer to that.
        """

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            latest = await self.get(request_id)
            if latest is not None and latest.status in TERMINAL_COMMAND_STATUSES:
                return latest
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest
            await asyncio.sleep(min(self.POLL_INTERVAL_SECONDS, remaining))

    async def stats(self) -> CommandStatusStats:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(COMMAND_STATUS_COUNT_BY_STATUS, POSTGRES_MARKER), (self._space_id,)
            )
            counts = {str(status): int(count) for status, count in await cursor.fetchall()}
            cursor = await conn.execute(
                render(COMMAND_STATUS_OLDEST_ACTIVE, POSTGRES_MARKER), (self._space_id,)
            )
            oldest = (await cursor.fetchone())[0]

        return CommandStatusStats(
            total=sum(counts.values()),
            accepted=counts.get("accepted", 0),
            retrying=counts.get("retrying", 0),
            applied=counts.get("applied", 0),
            failed=counts.get("failed", 0),
            # No file here, and the table's size belongs to every space in it.
            database_bytes=0,
            retention_days=self.retention_days,
            max_records=self.max_records,
            oldest_active_at=str(oldest) if oldest is not None else None,
        )

    async def prune(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
        deleted = 0
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(COMMAND_STATUS_PRUNE_EXPIRED, POSTGRES_MARKER),
                (self._space_id, cutoff),
            )
            deleted += max(0, cursor.rowcount)

            cursor = await conn.execute(
                render(COMMAND_STATUS_COUNT_ALL, POSTGRES_MARKER), (self._space_id,)
            )
            total = int((await cursor.fetchone())[0])
            overflow = max(0, total - self.max_records)
            if overflow:
                cursor = await conn.execute(
                    render(COMMAND_STATUS_PRUNE_OVERFLOW, POSTGRES_MARKER),
                    (self._space_id, self._space_id, overflow),
                )
                deleted += max(0, cursor.rowcount)
        return deleted

    async def _transition(
        self,
        request_id: str,
        kind: str,
        status: CommandStatus,
        resource_id: str | None,
        error: str | None,
    ) -> CommandStatusRecord:
        """Apply a status change, refusing the ones that would lose information.

        The precedence rules are the embedded ledger's, verbatim, because they are
        what stops a late `accepted` or a retry notification from overwriting a
        finished outcome. Held in one transaction so two replicas reporting on the
        same command serialise instead of interleaving read and write.
        """

        request_id = request_id.strip()
        kind = kind.strip()
        if not request_id or not kind:
            raise ValueError("request_id and kind are required")
        now = datetime.now(UTC).isoformat()

        async with self._pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    render(COMMAND_STATUS_SELECT, POSTGRES_MARKER) + " FOR UPDATE",
                    (self._space_id, request_id),
                )
                current = await cursor.fetchone()

                if current is not None:
                    existing = _command_status_from_row(current)
                    if existing.status == "applied" or (
                        existing.status == "failed" and status != "applied"
                    ):
                        return existing
                    if status == "accepted" and existing.status != "accepted":
                        return existing
                    attempts = existing.attempts + (1 if status != "accepted" else 0)
                    await conn.execute(
                        render(COMMAND_STATUS_UPDATE, POSTGRES_MARKER),
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
                    await conn.execute(
                        render(COMMAND_STATUS_INSERT, POSTGRES_MARKER),
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

        record = await self.get(request_id)
        assert record is not None
        await self._maybe_prune()
        return record

    async def _maybe_prune(self) -> None:
        self._writes_since_prune += 1
        if self._writes_since_prune < self.prune_every_writes:
            return
        self._writes_since_prune = 0
        await self.prune()


class PostgresCommitmentLedger:
    """Promises in play, shared across replicas.

    The state machine and merge rules are not here: they are in
    decide_commitment_apply, which the SQLite ledger calls too. This class does
    the reads, the writes, and the transaction — nothing that decides anything.
    That split is why there is one state machine rather than two.
    """

    def __init__(self, pool: Any, *, space_id: str | None = None) -> None:
        self._pool = pool
        # Unused: every method takes the space as an argument, matching the
        # embedded ledger's signature. Accepted so the router can construct all
        # ledgers the same way.
        self._space_id = space_id

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 8):
        pool_cls = require_pool_driver()
        pool = pool_cls(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        ledger = cls(pool)
        await ledger.ensure_schema()
        return ledger

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(COMMITMENTS_SCHEMA)
            await conn.execute(COMMITMENTS_INDEX)
            await conn.execute(COMMITMENT_REVISIONS_SCHEMA)
            await conn.execute(COMMITMENT_REVISIONS_INDEX)

    async def apply(self, intent: MemoryIntent) -> CommitmentApplyResult:
        """Read, decide, write — one transaction.

        FOR UPDATE on the existing row, so two replicas applying intents to the
        same commitment serialise instead of both computing a next revision from
        the same current one.
        """

        fields = _intent_fields(intent)
        now = datetime.now(UTC).isoformat()
        intent_hash = _intent_hash(intent)

        async with self._pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    render(COMMITMENT_REVISION_BY_INTENT, POSTGRES_MARKER),
                    (intent.intent_id,),
                )
                replay = await cursor.fetchone()
                if replay is not None:
                    stored = _commitment_revision_from_row(replay)
                    if stored.intent_hash != intent_hash:
                        raise CommitmentConflict(
                            "intent id reused with different commitment payload"
                        )
                    cursor = await conn.execute(
                        render(COMMITMENT_SELECT_BY_ID, POSTGRES_MARKER),
                        (stored.commitment_id,),
                    )
                    row = await cursor.fetchone()
                    assert row is not None
                    return CommitmentApplyResult(
                        commitment=_commitment_from_row(row),
                        revision=stored.record,
                        commitment_created=False,
                        revision_created=False,
                    )

                fields["identity_id"] = commitment_identity(
                    intent.memory_space_id,
                    fields["promisor"],
                    fields["predicate"],
                    fields["action"],
                    fields["beneficiaries"],
                )
                commitment_id = intent.target_id or fields["identity_id"]
                cursor = await conn.execute(
                    render(COMMITMENT_SELECT_BY_ID, POSTGRES_MARKER) + " FOR UPDATE",
                    (commitment_id,),
                )
                existing_row = await cursor.fetchone()
                existing = (
                    _commitment_from_row(existing_row) if existing_row is not None else None
                )
                if existing is not None:
                    fields["requested_status"] = _requested_status(intent, existing.status)

                decision = decide_commitment_apply(
                    intent,
                    existing=existing,
                    fields=fields,
                    now=now,
                    merge_values=_merge_values,
                    validate_identity=_validate_identity,
                )
                record = decision.record

                if decision.created:
                    await conn.execute(
                        render(COMMITMENT_INSERT, POSTGRES_MARKER),
                        _commitment_values(record),
                    )
                else:
                    await conn.execute(
                        render(COMMITMENT_UPDATE, POSTGRES_MARKER),
                        (
                            json.dumps(record.participants, ensure_ascii=False),
                            record.condition,
                            record.due_at,
                            record.status,
                            record.revision,
                            record.updated_at,
                            record.commitment_id,
                            record.memory_space_id,
                        ),
                    )

                revision_id = "commitment-revision:" + hashlib.sha256(
                    f"{record.commitment_id}\x1f{intent.intent_id}".encode()
                ).hexdigest()[:32]
                await conn.execute(
                    render(COMMITMENT_REVISION_INSERT, POSTGRES_MARKER),
                    (
                        revision_id,
                        record.commitment_id,
                        intent.intent_id,
                        intent_hash,
                        intent.source_event_id,
                        intent.authority,
                        intent.operation_hint or "add",
                        decision.previous_status,
                        record.status,
                        intent.raw_claim,
                        record.model_dump_json(),
                        now,
                    ),
                )
                cursor = await conn.execute(
                    render(COMMITMENT_REVISION_BY_ID, POSTGRES_MARKER), (revision_id,)
                )
                revision_row = await cursor.fetchone()

        return CommitmentApplyResult(
            commitment=record,
            revision=_commitment_revision_from_row(revision_row).record,
            commitment_created=decision.created,
            revision_created=True,
        )

    async def get(
        self, memory_space_id: str, commitment_id: str
    ) -> CommitmentRecord | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                render(COMMITMENT_SELECT_ONE, POSTGRES_MARKER),
                (memory_space_id, commitment_id),
            )
            row = await cursor.fetchone()
        return _commitment_from_row(row) if row is not None else None

    async def mark_projected(
        self,
        memory_space_id: str,
        commitment_id: str,
        revision: int,
        *,
        targets: set[str],
    ) -> None:
        columns = {"drawer": "drawer_projection_state", "kg": "kg_projection_state"}
        unknown = set(targets) - set(columns)
        if unknown:
            raise ValueError(f"unknown projection targets: {sorted(unknown)}")
        if not targets:
            return
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                commitment_mark_projected(
                    POSTGRES_MARKER, [columns[t] for t in targets]
                ),
                (memory_space_id, commitment_id, revision),
            )
            if cursor.rowcount != 1:
                raise CommitmentConflict(
                    "commitment revision changed before projection completed"
                )

    async def list_current(
        self, memory_space_id: str, *, include_terminal: bool = False, limit: int = 100
    ) -> list[CommitmentRecord]:
        page = await self.list_current_page(
            memory_space_id, include_terminal=include_terminal, limit=limit
        )
        return page.commitments

    async def list_current_page(
        self, memory_space_id: str, *, include_terminal: bool = False, limit: int = 100
    ) -> CommitmentListPage:
        bounded = max(1, min(int(limit), 200))
        async with self._pool.connection() as conn:
            if include_terminal:
                cursor = await conn.execute(
                    render(COMMITMENT_COUNT, POSTGRES_MARKER), (memory_space_id,)
                )
                total = int((await cursor.fetchone())[0])
                cursor = await conn.execute(
                    render(COMMITMENT_SELECT_PAGE, POSTGRES_MARKER),
                    (memory_space_id, bounded),
                )
            else:
                statuses = sorted(ACTIVE_COMMITMENT_STATUSES)
                cursor = await conn.execute(
                    commitment_count_active(POSTGRES_MARKER, len(statuses)),
                    (memory_space_id, *statuses),
                )
                total = int((await cursor.fetchone())[0])
                cursor = await conn.execute(
                    commitment_select_active_page(POSTGRES_MARKER, len(statuses)),
                    (memory_space_id, *statuses, bounded),
                )
            rows = await cursor.fetchall()

        found = [_commitment_from_row(row) for row in rows]
        return CommitmentListPage(
            commitments=found,
            total=total,
            limit=bounded,
            truncated=total > len(found),
        )

    async def history(
        self, memory_space_id: str, commitment_id: str, *, limit: int = 50
    ) -> list[CommitmentRevisionRecord]:
        bounded = max(1, min(int(limit), 200))
        async with self._pool.connection() as conn:
            # Ownership check first: history for a commitment in another space
            # must read as absent, not as a permission error, so a caller cannot
            # probe which ids exist elsewhere.
            cursor = await conn.execute(
                render(COMMITMENT_SELECT_ONE, POSTGRES_MARKER),
                (memory_space_id, commitment_id),
            )
            if await cursor.fetchone() is None:
                return []
            cursor = await conn.execute(
                render(COMMITMENT_REVISION_HISTORY, POSTGRES_MARKER),
                (commitment_id, bounded),
            )
            rows = await cursor.fetchall()
        return [_commitment_revision_from_row(row).record for row in reversed(rows)]


class _StoredCommitmentRevision:
    """A revision plus its intent hash, which the record type does not carry."""

    __slots__ = ("record", "intent_hash", "commitment_id")

    def __init__(self, record, intent_hash: str, commitment_id: str) -> None:
        self.record = record
        self.intent_hash = intent_hash
        self.commitment_id = commitment_id


def _commitment_from_row(row: Any) -> CommitmentRecord:
    """Read positionally, in the order the shared statements select."""

    values = dict(zip(COMMITMENT_COLUMNS, row, strict=True))
    return CommitmentRecord(
        commitment_id=str(values["commitment_id"]),
        memory_space_id=str(values["memory_space_id"]),
        promisor=str(values["promisor"]),
        predicate=str(values["predicate"]),
        action=str(values["action_value"]),
        beneficiaries=json.loads(values["beneficiaries_json"] or "[]"),
        participants=json.loads(values["participants_json"] or "[]"),
        condition=values["condition_value"] or None,
        due_at=values["due_at"] or None,
        status=str(values["status"]),
        revision=int(values["revision"]),
        created_at=str(values["created_at"]),
        updated_at=str(values["updated_at"]),
        drawer_projection_state=str(values["drawer_projection_state"]),
        kg_projection_state=str(values["kg_projection_state"]),
    )


def _commitment_revision_from_row(row: Any) -> _StoredCommitmentRevision:
    values = dict(zip(COMMITMENT_REVISION_COLUMNS, row, strict=True))
    return _StoredCommitmentRevision(
        record=CommitmentRevisionRecord(
            revision_id=str(values["revision_id"]),
            commitment_id=str(values["commitment_id"]),
            intent_id=str(values["intent_id"]),
            source_event_id=str(values["source_event_id"]),
            authority=str(values["authority"]),
            operation=str(values["operation"]),
            previous_status=values["previous_status"] or None,
            status=str(values["status"]),
            raw_claim=str(values["raw_claim"]),
            snapshot=CommitmentRecord.model_validate_json(str(values["snapshot_json"])),
            recorded_at=str(values["recorded_at"]),
        ),
        intent_hash=str(values["intent_hash"]),
        commitment_id=str(values["commitment_id"]),
    )


def _command_status_from_row(row: Any) -> CommandStatusRecord:
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


def _dlq_from_row(row: Any) -> DlqRecord:
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


def _decision_to_row(record: ExtractionDecisionRecord) -> tuple:
    """Ordered to match EXTRACTION_DECISION_COLUMNS."""

    created_at = record.created_at
    return (
        record.memory_space_id,
        record.source_turn_id,
        record.extractor_version,
        record.input_hash,
        record.decision.model_dump_json(),
        json.dumps(
            [intent.model_dump(mode="json") for intent in record.intents],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        created_at if isinstance(created_at, str) else created_at.isoformat(),
    )


def _decision_from_row(row: Any) -> ExtractionDecisionRecord:
    """Read positionally, in the order the statements select.

    psycopg returns tuples by default while sqlite3 is configured for name
    access, so the two implementations cannot share this. Selecting explicit
    columns rather than ``*`` is what keeps the positions meaningful.
    """

    values = dict(zip(EXTRACTION_DECISION_COLUMNS, row, strict=True))
    return ExtractionDecisionRecord(
        memory_space_id=str(values["memory_space_id"]),
        source_turn_id=str(values["source_turn_id"]),
        extractor_version=str(values["extractor_version"]),
        input_hash=str(values["input_hash"]),
        decision=StewardDecision.model_validate_json(str(values["decision_json"])),
        intents=[
            MemoryIntent.model_validate(item)
            for item in json.loads(str(values["intents_json"]))
        ],
        created_at=str(values["created_at"]),
    )
