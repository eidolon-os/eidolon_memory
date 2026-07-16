"""SQLite canonical exact-fact and evidence ledger."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from eidolon_sdk.memory import MemoryIntent

from eidolon.memory.domain.canonical_fact import (
    CanonicalEvidenceConflict,
    CanonicalFactRegistration,
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
            else:
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
            pending_targets=sorted(
                target
                for target in targets
                if projection_states[target] != "projected"
            ),
        )

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
