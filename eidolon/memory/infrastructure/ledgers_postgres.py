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

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from eidolon_memory_contracts import MemoryIntent

from eidolon.memory.domain.dlq import DlqRecord, DlqReplayItem, DlqStats
from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
)
from eidolon.memory.domain.steward import StewardDecision
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
    EXTRACTION_DECISION_COLUMNS,
    EXTRACTION_DECISION_INSERT,
    EXTRACTION_DECISION_SELECT,
    EXTRACTION_DECISIONS_SCHEMA,
    POSTGRES_MARKER,
    SYNC_EVENT_INSERT,
    SYNC_EVENT_SEEN,
    SYNC_EVENTS_INDEX,
    SYNC_EVENTS_SCHEMA,
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
        """

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

    async def release_stale_claims(self, *, older_than_seconds: int = 900) -> int:
        """Hand back claims whose worker never finished, and say how many.

        Replaces the embedded ledger's reset-on-open. Age is the only signal
        available here: a replica cannot distinguish another replica that died
        from one still working, so the threshold must exceed the longest replay a
        healthy worker performs.
        """

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
