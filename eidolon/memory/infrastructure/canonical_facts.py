"""SQLite canonical exact-fact and evidence ledger."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from eidolon_memory_contracts import MemoryIntent

from eidolon.memory.domain.canonical_fact import (
    CanonicalEvidenceConflict,
    CanonicalFactConflict,
    CanonicalFactEvidenceRecord,
    CanonicalFactHistoryRecord,
    CanonicalFactInactive,
    CanonicalFactInvalidation,
    CanonicalFactRecord,
    CanonicalFactRegistration,
    CanonicalFactStats,
    CanonicalFactTransitionRecord,
    ProjectionTarget,
    canonical_assertion_id,
)
from eidolon.memory.domain.predicates import PredicateCardinality, predicate_definition
from eidolon.memory.infrastructure.ledger_sql import (
    canonical_schema,
    ensure_ledger_schema_current,
)
from eidolon.memory.infrastructure.sqlite_writes import SerialisedSqliteWrites

_TARGET_COLUMNS: dict[ProjectionTarget, str] = {
    "drawer": "drawer_projection_state",
    "kg": "kg_projection_state",
}
_HISTORY_FACT_LIMIT = 100
_HISTORY_EVENT_LIMIT = 200


class CanonicalFactLedger(SerialisedSqliteWrites):
    """Own exact structured fact identity and confirmation provenance.

    This is canonical decision state, not a Chroma/KG projection. Only the
    Realm command worker writes it; recall does not query it on the hot path.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_write_lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            # Before creating, for the same reason as the other ledgers: a file
            # from before a column existed opens fine and fails on the first
            # statement. Checked on activation_count because it was the last
            # column added, so its absence means the file predates the current
            # shape. Verified against the four live palaces on this machine —
            # all four already carry it.
            ensure_ledger_schema_current(
                conn,
                table="canonical_assertions",
                required_column="activation_count",
                path=self.path,
            )
            for statement in canonical_schema():
                conn.execute(statement)

    async def register(
        self,
        intent: MemoryIntent,
        *,
        targets: set[ProjectionTarget],
    ) -> CanonicalFactRegistration:
        return await self._write(self._register_sync, intent, targets)

    async def mark_projected(
        self,
        memory_space_id: str,
        assertion_id: str,
        *,
        targets: set[ProjectionTarget],
    ) -> None:
        await self._read(
            self._set_projection_state_sync,
            memory_space_id,
            assertion_id,
            targets,
            "projected",
        )

    async def mark_projection_pending(
        self,
        memory_space_id: str,
        assertion_id: str,
        *,
        targets: set[ProjectionTarget],
    ) -> None:
        await self._read(
            self._set_projection_state_sync,
            memory_space_id,
            assertion_id,
            targets,
            "pending",
        )

    async def evidence_count(self, assertion_id: str) -> int:
        return await self._read(self._evidence_count_sync, assertion_id)

    async def active_for_slot(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
    ) -> list[CanonicalFactRecord]:
        return await self._read(
            self._active_for_slot_sync,
            memory_space_id,
            subject,
            predicate,
        )

    async def get_fact(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
        object_value: str,
    ) -> CanonicalFactRecord | None:
        return await self._read(
            self._get_fact_sync,
            memory_space_id,
            subject,
            predicate,
            object_value,
        )

    async def register_reactivation(
        self,
        intent: MemoryIntent,
        *,
        targets: set[ProjectionTarget],
    ) -> CanonicalFactRegistration:
        return await self._write(
            self._register_reactivation_sync,
            intent,
            targets,
        )

    async def mark_reactivated(
        self,
        memory_space_id: str,
        intent_id: str,
        *,
        targets: set[ProjectionTarget] | None = None,
    ) -> None:
        required = set(_TARGET_COLUMNS) if targets is None else set(targets)
        if not required or not required.issubset(_TARGET_COLUMNS):
            raise ValueError("canonical reactivation requires known projection targets")
        await self._write(
            self._mark_reactivated_sync,
            memory_space_id,
            intent_id,
            required,
        )

    async def register_invalidation(
        self,
        intent: MemoryIntent,
    ) -> CanonicalFactInvalidation:
        return await self._write(self._register_invalidation_sync, intent)

    async def mark_invalidated(
        self,
        memory_space_id: str,
        intent_id: str,
    ) -> None:
        await self._write(
            self._mark_invalidated_sync,
            memory_space_id,
            intent_id,
        )

    async def stats(self) -> CanonicalFactStats:
        return await self._read(self._stats_sync)

    async def history(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
        *,
        object_value: str | None = None,
        limit: int = 100,
    ) -> list[CanonicalFactHistoryRecord]:
        return await self._read(
            self._history_sync,
            memory_space_id,
            subject,
            predicate,
            object_value,
            limit,
        )

    def _register_sync(
        self,
        intent: MemoryIntent,
        targets: set[ProjectionTarget],
    ) -> CanonicalFactRegistration:
        if not intent.subject or not intent.predicate or not intent.object:
            raise ValueError("canonical fact registration requires a complete triple")
        if not targets or not targets.issubset(_TARGET_COLUMNS):
            raise ValueError("canonical fact registration requires known projection targets")
        assertion_id = canonical_assertion_id(
            intent.memory_space_id,
            intent.subject,
            intent.predicate,
            intent.object,
        )
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            assertion = conn.execute(
                "SELECT * FROM canonical_assertions WHERE assertion_id = ?",
                (assertion_id,),
            ).fetchone()
            definition = predicate_definition(intent.predicate)
            if assertion is None and definition.cardinality == PredicateCardinality.SINGLE:
                occupied = conn.execute(
                    """
                    SELECT assertion_id, object_value
                    FROM canonical_assertions
                    WHERE memory_space_id = ? AND subject = ? AND predicate = ?
                      AND state = 'active'
                    """,
                    (intent.memory_space_id, intent.subject, intent.predicate),
                ).fetchall()
                if occupied:
                    objects = ", ".join(str(row["object_value"]) for row in occupied)
                    raise CanonicalFactConflict(
                        f"single predicate slot is already active: {objects}"
                    )
            if assertion is None:
                conn.execute(
                    """
                    INSERT INTO canonical_assertions (
                        assertion_id, memory_space_id, subject, predicate,
                        object_value, state, drawer_projection_state,
                        kg_projection_state, evidence_count,
                        created_at, updated_at, last_confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', 'pending', 'pending', 0, ?, ?, ?)
                    """,
                    (
                        assertion_id,
                        intent.memory_space_id,
                        intent.subject,
                        intent.predicate,
                        intent.object,
                        now,
                        now,
                        now,
                    ),
                )
                projection_states = {"drawer": "pending", "kg": "pending"}
            else:
                if (
                    str(assertion["memory_space_id"]) != intent.memory_space_id
                    or str(assertion["subject"]) != intent.subject
                    or str(assertion["predicate"]) != intent.predicate
                    or str(assertion["object_value"]) != intent.object
                ):
                    raise CanonicalEvidenceConflict(
                        "canonical assertion id resolved to different fact fields"
                    )
                projection_states = {
                    target: str(assertion[column])
                    for target, column in _TARGET_COLUMNS.items()
                }

            existing_evidence = conn.execute(
                "SELECT * FROM canonical_evidence WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()
            evidence_created = existing_evidence is None
            if existing_evidence is not None:
                if (
                    str(existing_evidence["memory_space_id"])
                    != intent.memory_space_id
                    or str(existing_evidence["assertion_id"]) != assertion_id
                    or str(existing_evidence["source_event_id"])
                    != intent.source_event_id
                    or (existing_evidence["tool_call_id"] or None)
                    != intent.tool_call_id
                    or str(existing_evidence["authority"]) != intent.authority
                    or str(existing_evidence["raw_claim"]) != intent.raw_claim
                    or float(existing_evidence["confidence"]) != intent.confidence
                    or (existing_evidence["occurred_at"] or None)
                    != intent.occurred_at
                ):
                    raise CanonicalEvidenceConflict(
                        "intent id reused with different canonical evidence"
                    )
                if assertion is not None and str(assertion["state"]) != "active":
                    return CanonicalFactRegistration(
                        assertion_id=assertion_id,
                        memory_space_id=intent.memory_space_id,
                        intent_id=intent.intent_id,
                        evidence_count=int(assertion["evidence_count"]),
                        evidence_created=False,
                        state=str(assertion["state"]),
                        pending_targets=[],
                        projection_id=_projection_id(
                            assertion_id,
                            int(assertion["activation_count"]),
                        ),
                    )
            else:
                if assertion is not None and str(assertion["state"]) != "active":
                    raise CanonicalFactInactive(
                        "inactive canonical fact requires an explicit reactivation flow"
                    )
                conn.execute(
                    """
                    INSERT INTO canonical_evidence (
                        intent_id, memory_space_id, assertion_id, source_event_id,
                        tool_call_id, authority, raw_claim, confidence,
                        occurred_at, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.intent_id,
                        intent.memory_space_id,
                        assertion_id,
                        intent.source_event_id,
                        intent.tool_call_id,
                        intent.authority,
                        intent.raw_claim,
                        intent.confidence,
                        intent.occurred_at,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE canonical_assertions
                    SET evidence_count = evidence_count + 1,
                        updated_at = ?, last_confirmed_at = ?
                    WHERE assertion_id = ?
                    """,
                    (now, now, assertion_id),
                )

            row = conn.execute(
                "SELECT evidence_count FROM canonical_assertions WHERE assertion_id = ?",
                (assertion_id,),
            ).fetchone()
            evidence_count = int(row["evidence_count"])
        return CanonicalFactRegistration(
            assertion_id=assertion_id,
            memory_space_id=intent.memory_space_id,
            intent_id=intent.intent_id,
            evidence_count=evidence_count,
            evidence_created=evidence_created,
            state="active",
            pending_targets=sorted(
                target
                for target in targets
                if projection_states[target] != "projected"
            ),
            projection_id=_projection_id(
                assertion_id,
                1 if assertion is None else int(assertion["activation_count"]),
            ),
        )

    def _register_invalidation_sync(
        self,
        intent: MemoryIntent,
    ) -> CanonicalFactInvalidation:
        if (
            intent.intent_type != "correction"
            or intent.operation_hint != "invalidate"
            or not intent.subject
            or not intent.predicate
            or not intent.object
        ):
            raise ValueError(
                "canonical invalidation requires an exact correction triple"
            )
        assertion_id = canonical_assertion_id(
            intent.memory_space_id,
            intent.subject,
            intent.predicate,
            intent.object,
        )
        now = datetime.now(UTC).isoformat()
        ended_at = intent.occurred_at or now
        reason = str(intent.attributes.get("reason") or "")
        result_state = str(intent.attributes.get("result_state") or "invalidated")
        if result_state not in {"invalidated", "superseded"}:
            raise ValueError("canonical invalidation result_state is invalid")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            assertion = conn.execute(
                "SELECT * FROM canonical_assertions WHERE assertion_id = ?",
                (assertion_id,),
            ).fetchone()
            if assertion is None:
                return CanonicalFactInvalidation(
                    assertion_id=assertion_id,
                    memory_space_id=intent.memory_space_id,
                    intent_id=intent.intent_id,
                    matched=False,
                )
            if str(assertion["memory_space_id"]) != intent.memory_space_id:
                raise CanonicalEvidenceConflict(
                    "canonical invalidation resolved outside memory space"
                )

            existing = conn.execute(
                "SELECT * FROM canonical_invalidations WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()
            created = existing is None
            if existing is not None:
                if (
                    str(existing["memory_space_id"]) != intent.memory_space_id
                    or str(existing["assertion_id"]) != assertion_id
                    or str(existing["source_event_id"]) != intent.source_event_id
                    or str(existing["raw_claim"]) != intent.raw_claim
                    or (
                        intent.occurred_at is not None
                        and str(existing["ended_at"]) != ended_at
                    )
                    or str(existing["reason"]) != reason
                    or str(existing["result_state"]) != result_state
                ):
                    raise CanonicalEvidenceConflict(
                        "intent id reused with different canonical invalidation"
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO canonical_invalidations (
                        intent_id, memory_space_id, assertion_id, source_event_id,
                        raw_claim, ended_at, reason, recorded_at, state, result_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        intent.intent_id,
                        intent.memory_space_id,
                        assertion_id,
                        intent.source_event_id,
                        intent.raw_claim,
                        ended_at,
                        reason,
                        now,
                        result_state,
                    ),
                )
            count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM canonical_invalidations
                    WHERE assertion_id = ?
                    """,
                    (assertion_id,),
                ).fetchone()[0]
            )
        return CanonicalFactInvalidation(
            assertion_id=assertion_id,
            memory_space_id=intent.memory_space_id,
            intent_id=intent.intent_id,
            matched=True,
            invalidation_created=created,
            invalidation_count=count,
            state=(
                "pending" if existing is None else str(existing["state"])
            ),
            result_state=result_state,
            projection_id=_projection_id(
                assertion_id,
                int(assertion["activation_count"]),
            ),
        )

    def _register_reactivation_sync(
        self,
        intent: MemoryIntent,
        targets: set[ProjectionTarget],
    ) -> CanonicalFactRegistration:
        if (
            intent.operation_hint != "update"
            or not intent.subject
            or not intent.predicate
            or not intent.object
        ):
            raise ValueError("canonical reactivation requires an exact update triple")
        if not targets or not targets.issubset(_TARGET_COLUMNS):
            raise ValueError("canonical reactivation requires known projection targets")
        assertion_id = canonical_assertion_id(
            intent.memory_space_id,
            intent.subject,
            intent.predicate,
            intent.object,
        )
        now = datetime.now(UTC).isoformat()
        reactivated_at = intent.occurred_at or now
        reason = str(intent.attributes.get("reason") or "explicit reactivation")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            assertion = conn.execute(
                "SELECT * FROM canonical_assertions WHERE assertion_id = ?",
                (assertion_id,),
            ).fetchone()
            if assertion is None:
                raise LookupError("canonical assertion not found for reactivation")
            if str(assertion["memory_space_id"]) != intent.memory_space_id:
                raise CanonicalEvidenceConflict(
                    "canonical reactivation resolved outside memory space"
                )

            existing = conn.execute(
                "SELECT * FROM canonical_reactivations WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["memory_space_id"]) != intent.memory_space_id
                    or str(existing["assertion_id"]) != assertion_id
                    or str(existing["source_event_id"]) != intent.source_event_id
                    or str(existing["raw_claim"]) != intent.raw_claim
                    or str(existing["reactivated_at"]) != reactivated_at
                    or str(existing["reason"]) != reason
                ):
                    raise CanonicalEvidenceConflict(
                        "intent id reused with different canonical reactivation"
                    )
                activation_number = int(existing["activation_number"])
                projection_states = {
                    target: str(assertion[column])
                    for target, column in _TARGET_COLUMNS.items()
                }
                event_state = str(existing["state"])
                return CanonicalFactRegistration(
                    assertion_id=assertion_id,
                    memory_space_id=intent.memory_space_id,
                    intent_id=intent.intent_id,
                    evidence_count=int(assertion["evidence_count"]),
                    evidence_created=False,
                    state=(
                        "active"
                        if event_state == "applied"
                        else str(assertion["state"])
                    ),
                    pending_targets=(
                        []
                        if event_state == "applied"
                        else sorted(
                            target
                            for target in targets
                            if projection_states[target] != "projected"
                        )
                    ),
                    projection_id=_projection_id(assertion_id, activation_number),
                    reactivation_pending=event_state == "pending",
                )

            prior_state = str(assertion["state"])
            if prior_state == "active":
                raise CanonicalFactConflict("canonical assertion is already active")
            if prior_state not in {"invalidated", "superseded"}:
                raise CanonicalFactConflict(
                    f"canonical assertion cannot reactivate from {prior_state}"
                )
            definition = predicate_definition(intent.predicate)
            if definition.cardinality == PredicateCardinality.SINGLE:
                occupied = conn.execute(
                    """
                    SELECT object_value FROM canonical_assertions
                    WHERE memory_space_id = ? AND subject = ? AND predicate = ?
                      AND state = 'active' AND assertion_id != ?
                    """,
                    (
                        intent.memory_space_id,
                        intent.subject,
                        intent.predicate,
                        assertion_id,
                    ),
                ).fetchall()
                if occupied:
                    raise CanonicalFactConflict(
                        "single predicate slot must be vacated before reactivation"
                    )

            existing_evidence = conn.execute(
                "SELECT * FROM canonical_evidence WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()
            if existing_evidence is not None:
                raise CanonicalEvidenceConflict(
                    "reactivation intent id already belongs to canonical evidence"
                )
            activation_number = int(assertion["activation_count"]) + 1
            conn.execute(
                """
                INSERT INTO canonical_reactivations (
                    intent_id, memory_space_id, assertion_id, source_event_id,
                    raw_claim, reactivated_at, reason, prior_state,
                    activation_number, recorded_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    intent.intent_id,
                    intent.memory_space_id,
                    assertion_id,
                    intent.source_event_id,
                    intent.raw_claim,
                    reactivated_at,
                    reason,
                    prior_state,
                    activation_number,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO canonical_evidence (
                    intent_id, memory_space_id, assertion_id, source_event_id,
                    tool_call_id, authority, raw_claim, confidence,
                    occurred_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.intent_id,
                    intent.memory_space_id,
                    assertion_id,
                    intent.source_event_id,
                    intent.tool_call_id,
                    intent.authority,
                    intent.raw_claim,
                    intent.confidence,
                    intent.occurred_at,
                    now,
                ),
            )
            assignments = ", ".join(
                f"{_TARGET_COLUMNS[target]} = 'pending'" for target in sorted(targets)
            )
            conn.execute(
                f"""
                UPDATE canonical_assertions
                SET evidence_count = evidence_count + 1,
                    updated_at = ?, last_confirmed_at = ?, {assignments}
                WHERE assertion_id = ?
                """,
                (now, now, assertion_id),
            )
            evidence_count = int(assertion["evidence_count"]) + 1
        return CanonicalFactRegistration(
            assertion_id=assertion_id,
            memory_space_id=intent.memory_space_id,
            intent_id=intent.intent_id,
            evidence_count=evidence_count,
            evidence_created=True,
            state=prior_state,
            pending_targets=sorted(targets),
            projection_id=_projection_id(assertion_id, activation_number),
            reactivation_pending=True,
        )

    def _mark_reactivated_sync(
        self,
        memory_space_id: str,
        intent_id: str,
        targets: set[ProjectionTarget],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute(
                "SELECT * FROM canonical_reactivations WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if event is None:
                raise LookupError("canonical reactivation not registered")
            if str(event["memory_space_id"]) != memory_space_id:
                raise CanonicalEvidenceConflict(
                    "canonical reactivation belongs to another memory space"
                )
            if str(event["state"]) == "applied":
                return
            assertion_id = str(event["assertion_id"])
            assertion = conn.execute(
                "SELECT * FROM canonical_assertions WHERE assertion_id = ?",
                (assertion_id,),
            ).fetchone()
            if assertion is None:
                raise LookupError("canonical assertion not found for reactivation")
            pending = [
                target
                for target in sorted(targets)
                if str(assertion[_TARGET_COLUMNS[target]]) != "projected"
            ]
            if pending:
                raise RuntimeError(
                    "canonical reactivation projections are still pending: "
                    + ",".join(sorted(pending))
                )
            result = conn.execute(
                """
                UPDATE canonical_assertions
                SET state = 'active', activation_count = ?, updated_at = ?
                WHERE assertion_id = ? AND memory_space_id = ?
                  AND state IN ('invalidated', 'superseded')
                """,
                (
                    int(event["activation_number"]),
                    now,
                    assertion_id,
                    memory_space_id,
                ),
            )
            if result.rowcount != 1:
                raise CanonicalFactConflict(
                    "canonical assertion changed before reactivation completed"
                )
            conn.execute(
                "UPDATE canonical_reactivations SET state = 'applied' WHERE intent_id = ?",
                (intent_id,),
            )

    def _mark_invalidated_sync(
        self,
        memory_space_id: str,
        intent_id: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            invalidation = conn.execute(
                """
                SELECT assertion_id, memory_space_id, state, result_state
                FROM canonical_invalidations
                WHERE intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            if invalidation is None:
                raise LookupError("canonical invalidation not registered")
            if str(invalidation["memory_space_id"]) != memory_space_id:
                raise CanonicalEvidenceConflict(
                    "canonical invalidation belongs to another memory space"
                )
            if str(invalidation["state"]) == "applied":
                return
            assertion_id = str(invalidation["assertion_id"])
            result_state = str(invalidation["result_state"])
            if result_state not in {"invalidated", "superseded"}:
                raise ValueError("canonical invalidation result_state is invalid")
            conn.execute(
                """
                UPDATE canonical_invalidations SET state = 'applied'
                WHERE intent_id = ?
                """,
                (intent_id,),
            )
            result = conn.execute(
                """
                UPDATE canonical_assertions
                SET state = ?, updated_at = ?
                WHERE assertion_id = ? AND memory_space_id = ?
                """,
                (result_state, now, assertion_id, memory_space_id),
            )
            if result.rowcount != 1:
                raise LookupError("canonical assertion not found in memory space")

    def _set_projection_state_sync(
        self,
        memory_space_id: str,
        assertion_id: str,
        targets: set[ProjectionTarget],
        state: str,
    ) -> None:
        if state not in {"pending", "projected"}:
            raise ValueError("invalid canonical projection state")
        if not targets or not targets.issubset(_TARGET_COLUMNS):
            raise ValueError("canonical projection update requires known targets")
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            assignments = ", ".join(
                f"{_TARGET_COLUMNS[target]} = ?" for target in sorted(targets)
            )
            values = [state for _target in sorted(targets)]
            result = conn.execute(
                f"""
                UPDATE canonical_assertions
                SET {assignments}, updated_at = ?
                WHERE assertion_id = ? AND memory_space_id = ?
                """,
                (*values, now, assertion_id, memory_space_id),
            )
            if result.rowcount != 1:
                raise LookupError("canonical assertion not found in memory space")

    def _evidence_count_sync(self, assertion_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM canonical_evidence WHERE assertion_id = ?",
                (assertion_id,),
            ).fetchone()
        return int(row["count"])

    def _active_for_slot_sync(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
    ) -> list[CanonicalFactRecord]:
        predicate_definition(predicate)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT assertion_id, memory_space_id, subject, predicate,
                       object_value, state, activation_count
                FROM canonical_assertions
                WHERE memory_space_id = ? AND subject = ? AND predicate = ?
                  AND state = 'active'
                ORDER BY created_at, assertion_id
                """,
                (memory_space_id, subject, predicate),
            ).fetchall()
        return [
            _fact_record(row)
            for row in rows
        ]

    def _get_fact_sync(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
        object_value: str,
    ) -> CanonicalFactRecord | None:
        predicate_definition(predicate)
        assertion_id = canonical_assertion_id(
            memory_space_id,
            subject,
            predicate,
            object_value,
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT assertion_id, memory_space_id, subject, predicate,
                       object_value, state, activation_count
                FROM canonical_assertions
                WHERE assertion_id = ? AND memory_space_id = ?
                """,
                (assertion_id, memory_space_id),
            ).fetchone()
        return _fact_record(row) if row is not None else None

    def _history_sync(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
        object_value: str | None,
        limit: int,
    ) -> list[CanonicalFactHistoryRecord]:
        predicate_definition(predicate)
        bounded_limit = max(1, min(int(limit), _HISTORY_FACT_LIMIT))
        clauses = [
            "memory_space_id = ?",
            "subject = ?",
            "predicate = ?",
        ]
        params: list[object] = [memory_space_id, subject, predicate]
        if object_value is not None:
            clauses.append("object_value = ?")
            params.append(object_value)
        with self._connect() as conn:
            assertions = conn.execute(
                f"""
                SELECT * FROM canonical_assertions
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, assertion_id
                LIMIT ?
                """,
                (*params, bounded_limit),
            ).fetchall()
            result: list[CanonicalFactHistoryRecord] = []
            for assertion in assertions:
                assertion_id = str(assertion["assertion_id"])
                evidence_rows = conn.execute(
                    """
                    SELECT * FROM canonical_evidence
                    WHERE assertion_id = ?
                    ORDER BY recorded_at DESC, intent_id DESC
                    LIMIT ?
                    """,
                    (assertion_id, _HISTORY_EVENT_LIMIT + 1),
                ).fetchall()
                invalidation_rows = conn.execute(
                    """
                    SELECT * FROM canonical_invalidations
                    WHERE assertion_id = ? AND state = 'applied'
                    ORDER BY recorded_at DESC, intent_id DESC
                    LIMIT ?
                    """,
                    (assertion_id, _HISTORY_EVENT_LIMIT + 1),
                ).fetchall()
                reactivation_rows = conn.execute(
                    """
                    SELECT * FROM canonical_reactivations
                    WHERE assertion_id = ? AND state = 'applied'
                    ORDER BY recorded_at DESC, intent_id DESC
                    LIMIT ?
                    """,
                    (assertion_id, _HISTORY_EVENT_LIMIT + 1),
                ).fetchall()
                evidence_capped = len(evidence_rows) > _HISTORY_EVENT_LIMIT
                evidence_rows = list(
                    reversed(evidence_rows[:_HISTORY_EVENT_LIMIT])
                )
                transitions = [
                    CanonicalFactTransitionRecord(
                        intent_id=str(row["intent_id"]),
                        transition=str(row["result_state"]),
                        occurred_at=str(row["ended_at"]),
                        recorded_at=str(row["recorded_at"]),
                        reason=str(row["reason"]),
                        from_state="active",
                        to_state=str(row["result_state"]),
                    )
                    for row in invalidation_rows
                ]
                transitions.extend(
                    CanonicalFactTransitionRecord(
                        intent_id=str(row["intent_id"]),
                        transition="reactivated",
                        occurred_at=str(row["reactivated_at"]),
                        recorded_at=str(row["recorded_at"]),
                        reason=str(row["reason"]),
                        from_state=str(row["prior_state"]),
                        to_state="active",
                    )
                    for row in reactivation_rows
                )
                transitions.sort(key=lambda row: (row.recorded_at, row.intent_id))
                transitions_capped = len(transitions) > _HISTORY_EVENT_LIMIT
                transitions = transitions[-_HISTORY_EVENT_LIMIT:]
                result.append(
                    CanonicalFactHistoryRecord(
                        fact=_fact_record(assertion),
                        created_at=str(assertion["created_at"]),
                        updated_at=str(assertion["updated_at"]),
                        last_confirmed_at=str(assertion["last_confirmed_at"]),
                        evidence=[
                            CanonicalFactEvidenceRecord(
                                intent_id=str(row["intent_id"]),
                                source_event_id=str(row["source_event_id"]),
                                authority=str(row["authority"]),
                                raw_claim=str(row["raw_claim"]),
                                confidence=float(row["confidence"]),
                                occurred_at=row["occurred_at"] or None,
                                recorded_at=str(row["recorded_at"]),
                            )
                            for row in evidence_rows
                        ],
                        transitions=transitions,
                        evidence_capped=evidence_capped,
                        transitions_capped=transitions_capped,
                    )
                )
        return result

    def _stats_sync(self) -> CanonicalFactStats:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS assertions_total,
                    COALESCE(SUM(state = 'active'), 0) AS assertions_active,
                    COALESCE(SUM(state = 'invalidated'), 0)
                        AS assertions_invalidated,
                    COALESCE(SUM(state = 'superseded'), 0)
                        AS assertions_superseded,
                    COALESCE(SUM(
                        state = 'active' AND drawer_projection_state = 'pending'
                    ), 0)
                        AS drawer_not_projected,
                    COALESCE(SUM(
                        state = 'active' AND drawer_projection_state = 'projected'
                    ), 0)
                        AS drawer_projected,
                    COALESCE(SUM(
                        state = 'active' AND kg_projection_state = 'pending'
                    ), 0)
                        AS kg_not_projected,
                    COALESCE(SUM(
                        state = 'active' AND kg_projection_state = 'projected'
                    ), 0)
                        AS kg_projected
                FROM canonical_assertions
                """
            ).fetchone()
            evidence_total = int(
                conn.execute("SELECT COUNT(*) FROM canonical_evidence").fetchone()[0]
            )
            invalidations_total = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM canonical_invalidations
                    WHERE state = 'applied' AND result_state = 'invalidated'
                    """
                ).fetchone()[0]
            )
            supersessions_total = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM canonical_invalidations
                    WHERE state = 'applied' AND result_state = 'superseded'
                    """
                ).fetchone()[0]
            )
            reactivations_total = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM canonical_reactivations
                    WHERE state = 'applied'
                    """
                ).fetchone()[0]
            )
            reactivations_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM canonical_reactivations
                    WHERE state = 'pending'
                    """
                ).fetchone()[0]
            )
            invalidations_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM canonical_invalidations
                    WHERE state = 'pending' AND result_state = 'invalidated'
                    """
                ).fetchone()[0]
            )
            supersessions_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM canonical_invalidations
                    WHERE state = 'pending' AND result_state = 'superseded'
                    """
                ).fetchone()[0]
            )
        return CanonicalFactStats(
            assertions_total=int(row["assertions_total"]),
            assertions_active=int(row["assertions_active"]),
            assertions_invalidated=int(row["assertions_invalidated"]),
            assertions_superseded=int(row["assertions_superseded"]),
            evidence_total=evidence_total,
            invalidations_total=invalidations_total,
            supersessions_total=supersessions_total,
            reactivations_total=reactivations_total,
            reactivations_pending=reactivations_pending,
            invalidations_pending=invalidations_pending,
            supersessions_pending=supersessions_pending,
            drawer_not_projected=int(row["drawer_not_projected"]),
            drawer_projected=int(row["drawer_projected"]),
            kg_not_projected=int(row["kg_not_projected"]),
            kg_projected=int(row["kg_projected"]),
            database_bytes=self.path.stat().st_size if self.path.exists() else 0,
        )


def _projection_id(assertion_id: str, activation_count: int) -> str:
    if activation_count <= 1:
        return assertion_id
    return f"{assertion_id}:activation:{activation_count}"


def _fact_record(row: sqlite3.Row) -> CanonicalFactRecord:
    assertion_id = str(row["assertion_id"])
    activation_count = int(row["activation_count"])
    return CanonicalFactRecord(
        assertion_id=assertion_id,
        memory_space_id=str(row["memory_space_id"]),
        subject=str(row["subject"]),
        predicate=str(row["predicate"]),
        object=str(row["object_value"]),
        state=str(row["state"]),
        projection_id=_projection_id(assertion_id, activation_count),
    )
