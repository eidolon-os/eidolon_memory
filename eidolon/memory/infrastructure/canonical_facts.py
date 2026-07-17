"""SQLite canonical exact-fact and evidence ledger."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from eidolon_sdk.memory import MemoryIntent

from eidolon.memory.domain.canonical_fact import (
    CanonicalEvidenceConflict,
    CanonicalFactInactive,
    CanonicalFactInvalidation,
    CanonicalFactRegistration,
    CanonicalFactStats,
    ProjectionTarget,
    canonical_assertion_id,
)

_TARGET_COLUMNS: dict[ProjectionTarget, str] = {
    "drawer": "drawer_projection_state",
    "kg": "kg_projection_state",
}


class CanonicalFactLedger:
    """Own exact structured fact identity and confirmation provenance.

    This is canonical decision state, not a Chroma/KG projection. Only the
    Realm command worker writes it; recall does not query it on the hot path.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS canonical_assertions (
                    assertion_id TEXT PRIMARY KEY,
                    memory_space_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object_value TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    drawer_projection_state TEXT NOT NULL DEFAULT 'pending',
                    kg_projection_state TEXT NOT NULL DEFAULT 'pending',
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL,
                    UNIQUE(memory_space_id, subject, predicate, object_value)
                );
                CREATE TABLE IF NOT EXISTS canonical_evidence (
                    intent_id TEXT PRIMARY KEY,
                    memory_space_id TEXT NOT NULL,
                    assertion_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    tool_call_id TEXT,
                    authority TEXT NOT NULL,
                    raw_claim TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    occurred_at TEXT,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(assertion_id)
                        REFERENCES canonical_assertions(assertion_id)
                );
                CREATE INDEX IF NOT EXISTS idx_canonical_evidence_assertion
                    ON canonical_evidence(assertion_id, recorded_at);
                CREATE TABLE IF NOT EXISTS canonical_invalidations (
                    intent_id TEXT PRIMARY KEY,
                    memory_space_id TEXT NOT NULL,
                    assertion_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    raw_claim TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    FOREIGN KEY(assertion_id)
                        REFERENCES canonical_assertions(assertion_id)
                );
                CREATE INDEX IF NOT EXISTS idx_canonical_invalidations_assertion
                    ON canonical_invalidations(assertion_id, recorded_at);
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(canonical_assertions)")
            }
            added_target_columns: list[str] = []
            for column in _TARGET_COLUMNS.values():
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE canonical_assertions ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT 'pending'"
                    )
                    added_target_columns.append(column)
            if "projection_state" in columns:
                for column in added_target_columns:
                    conn.execute(
                        f"UPDATE canonical_assertions SET {column} = 'projected' "
                        "WHERE projection_state = 'projected'"
                    )
            invalidation_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(canonical_invalidations)")
            }
            if "state" not in invalidation_columns:
                conn.execute(
                    """
                    ALTER TABLE canonical_invalidations
                    ADD COLUMN state TEXT NOT NULL DEFAULT 'applied'
                    """
                )

    async def register(
        self,
        intent: MemoryIntent,
        *,
        targets: set[ProjectionTarget],
    ) -> CanonicalFactRegistration:
        return await asyncio.to_thread(self._register_sync, intent, targets)

    async def mark_projected(
        self,
        memory_space_id: str,
        assertion_id: str,
        *,
        targets: set[ProjectionTarget],
    ) -> None:
        await asyncio.to_thread(
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
        await asyncio.to_thread(
            self._set_projection_state_sync,
            memory_space_id,
            assertion_id,
            targets,
            "pending",
        )

    async def evidence_count(self, assertion_id: str) -> int:
        return await asyncio.to_thread(self._evidence_count_sync, assertion_id)

    async def register_invalidation(
        self,
        intent: MemoryIntent,
    ) -> CanonicalFactInvalidation:
        return await asyncio.to_thread(self._register_invalidation_sync, intent)

    async def mark_invalidated(
        self,
        memory_space_id: str,
        intent_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._mark_invalidated_sync,
            memory_space_id,
            intent_id,
        )

    async def stats(self) -> CanonicalFactStats:
        return await asyncio.to_thread(self._stats_sync)

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
                if assertion is not None and str(assertion["state"]) == "invalidated":
                    return CanonicalFactRegistration(
                        assertion_id=assertion_id,
                        memory_space_id=intent.memory_space_id,
                        intent_id=intent.intent_id,
                        evidence_count=int(assertion["evidence_count"]),
                        evidence_created=False,
                        state="invalidated",
                        pending_targets=[],
                    )
            else:
                if assertion is not None and str(assertion["state"]) == "invalidated":
                    raise CanonicalFactInactive(
                        "invalidated canonical fact requires an explicit reactivation flow"
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
                ):
                    raise CanonicalEvidenceConflict(
                        "intent id reused with different canonical invalidation"
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO canonical_invalidations (
                        intent_id, memory_space_id, assertion_id, source_event_id,
                        raw_claim, ended_at, reason, recorded_at, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
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
                SELECT assertion_id, memory_space_id, state
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
                SET state = 'invalidated', updated_at = ?
                WHERE assertion_id = ? AND memory_space_id = ?
                """,
                (now, assertion_id, memory_space_id),
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

    def _stats_sync(self) -> CanonicalFactStats:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS assertions_total,
                    COALESCE(SUM(state = 'active'), 0) AS assertions_active,
                    COALESCE(SUM(state = 'invalidated'), 0)
                        AS assertions_invalidated,
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
                    "SELECT COUNT(*) FROM canonical_invalidations WHERE state = 'applied'"
                ).fetchone()[0]
            )
            invalidations_pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM canonical_invalidations WHERE state = 'pending'"
                ).fetchone()[0]
            )
        return CanonicalFactStats(
            assertions_total=int(row["assertions_total"]),
            assertions_active=int(row["assertions_active"]),
            assertions_invalidated=int(row["assertions_invalidated"]),
            evidence_total=evidence_total,
            invalidations_total=invalidations_total,
            invalidations_pending=invalidations_pending,
            drawer_not_projected=int(row["drawer_not_projected"]),
            drawer_projected=int(row["drawer_projected"]),
            kg_not_projected=int(row["kg_not_projected"]),
            kg_projected=int(row["kg_projected"]),
            database_bytes=self.path.stat().st_size if self.path.exists() else 0,
        )
