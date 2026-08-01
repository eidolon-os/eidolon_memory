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
from typing import Any

from eidolon_memory_contracts import MemoryIntent

from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
)
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.ledger_sql import (
    EXTRACTION_DECISION_COLUMNS,
    EXTRACTION_DECISION_INSERT,
    EXTRACTION_DECISION_SELECT,
    EXTRACTION_DECISIONS_SCHEMA,
    POSTGRES_MARKER,
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
