"""Realm-local SQLite commitment state and immutable revision history."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eidolon_memory_contracts import MemoryIntent

from eidolon.memory.domain.commitment import (
    ACTIVE_COMMITMENT_STATUSES,
    COMMITMENT_PREDICATES,
    TERMINAL_COMMITMENT_STATUSES,
    CommitmentApplyResult,
    CommitmentConflict,
    CommitmentListPage,
    CommitmentRecord,
    CommitmentRevisionRecord,
    CommitmentStatus,
    commitment_identity,
)
from eidolon.memory.infrastructure.sqlite_writes import SerialisedSqliteWrites

_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"proposed", "confirmed", "cancelled", "superseded"}),
    "confirmed": frozenset({"confirmed", "fulfilled", "cancelled", "superseded"}),
    "fulfilled": frozenset({"fulfilled"}),
    "cancelled": frozenset({"cancelled"}),
    "superseded": frozenset({"superseded"}),
}


class CommitmentLedger(SerialisedSqliteWrites):
    """Single-writer commitment aggregate; separate from canonical facts."""

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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS commitments (
                    commitment_id TEXT PRIMARY KEY,
                    memory_space_id TEXT NOT NULL,
                    promisor TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    action_value TEXT NOT NULL,
                    beneficiaries_json TEXT NOT NULL,
                    participants_json TEXT NOT NULL,
                    condition_value TEXT,
                    due_at TEXT,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                    , drawer_projection_state TEXT NOT NULL DEFAULT 'pending'
                    , kg_projection_state TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS idx_commitments_realm_status
                    ON commitments(memory_space_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS commitment_revisions (
                    revision_id TEXT PRIMARY KEY,
                    commitment_id TEXT NOT NULL,
                    intent_id TEXT UNIQUE NOT NULL,
                    intent_hash TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    previous_status TEXT,
                    status TEXT NOT NULL,
                    raw_claim TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(commitment_id) REFERENCES commitments(commitment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_commitment_revisions_commitment
                    ON commitment_revisions(commitment_id, recorded_at);
                """
            )

    async def apply(self, intent: MemoryIntent) -> CommitmentApplyResult:
        return await self._write(self._apply_sync, intent)

    async def get(
        self, memory_space_id: str, commitment_id: str
    ) -> CommitmentRecord | None:
        return await self._read(self._get_sync, memory_space_id, commitment_id)

    async def mark_projected(
        self,
        memory_space_id: str,
        commitment_id: str,
        revision: int,
        *,
        targets: set[str],
    ) -> None:
        await self._write(
            self._mark_projected_sync,
            memory_space_id,
            commitment_id,
            revision,
            targets,
        )

    async def list_current(
        self,
        memory_space_id: str,
        *,
        include_terminal: bool = False,
        limit: int = 100,
    ) -> list[CommitmentRecord]:
        page = await self.list_current_page(
            memory_space_id,
            include_terminal=include_terminal,
            limit=limit,
        )
        return page.commitments

    async def list_current_page(
        self,
        memory_space_id: str,
        *,
        include_terminal: bool = False,
        limit: int = 100,
    ) -> CommitmentListPage:
        return await self._read(
            self._list_current_page_sync,
            memory_space_id,
            include_terminal,
            limit,
        )

    async def history(
        self, memory_space_id: str, commitment_id: str, *, limit: int = 200
    ) -> list[CommitmentRevisionRecord]:
        return await self._read(self._history_sync, memory_space_id, commitment_id, limit
        )

    def _apply_sync(self, intent: MemoryIntent) -> CommitmentApplyResult:
        fields = _intent_fields(intent)
        now = datetime.now(UTC).isoformat()
        intent_hash = _intent_hash(intent)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM commitment_revisions WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()
            if replay is not None:
                if str(replay["intent_hash"]) != intent_hash:
                    raise CommitmentConflict(
                        "intent id reused with different commitment payload"
                    )
                record = self._load_commitment(conn, str(replay["commitment_id"]))
                return CommitmentApplyResult(
                    commitment=record,
                    revision=_revision_record(replay),
                    commitment_created=False,
                    revision_created=False,
                )

            identity_id = commitment_identity(
                intent.memory_space_id,
                fields["promisor"],
                fields["predicate"],
                fields["action"],
                fields["beneficiaries"],
            )
            commitment_id = intent.target_id or identity_id
            existing = conn.execute(
                "SELECT * FROM commitments WHERE commitment_id = ?",
                (commitment_id,),
            ).fetchone()
            if intent.target_id and existing is None:
                raise CommitmentConflict("target commitment does not exist")
            if existing is None and intent.operation_hint not in {"add", "confirm", None}:
                raise CommitmentConflict("commitment update requires an existing target")

            created = existing is None
            previous_status: CommitmentStatus | None = None
            if existing is None:
                status: CommitmentStatus = (
                    "confirmed" if intent.operation_hint == "confirm" else "proposed"
                )
                beneficiaries = fields["beneficiaries"]
                participants = fields["participants"]
                condition = fields["condition"]
                due_at = fields["due_at"]
                revision_number = 1
                created_at = now
            else:
                beneficiaries = (
                    fields["beneficiaries"]
                    if fields["beneficiaries_provided"]
                    else _json_list(existing["beneficiaries_json"])
                )
                fields["beneficiaries"] = beneficiaries
                _validate_identity(existing, intent, fields)
                previous_status = str(existing["status"])
                status = _requested_status(intent, previous_status)
                if status not in _TRANSITIONS[previous_status]:
                    raise CommitmentConflict(
                        f"invalid commitment transition: {previous_status} -> {status}"
                    )
                participants = _merge_values(
                    _json_list(existing["participants_json"]),
                    fields["participants"],
                )
                condition = (
                    fields["condition"]
                    if "condition" in intent.attributes
                    else existing["condition_value"]
                )
                due_at = (
                    fields["due_at"]
                    if "due_at" in intent.attributes
                    else existing["due_at"]
                )
                revision_number = int(existing["revision"]) + 1
                created_at = str(existing["created_at"])

            record = CommitmentRecord(
                commitment_id=commitment_id,
                memory_space_id=intent.memory_space_id,
                promisor=fields["promisor"],
                predicate=fields["predicate"],
                action=fields["action"],
                beneficiaries=beneficiaries,
                participants=participants,
                condition=condition,
                due_at=due_at,
                status=status,
                revision=revision_number,
                created_at=created_at,
                updated_at=now,
            )
            snapshot_json = record.model_dump_json()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO commitments (
                        commitment_id, memory_space_id, promisor, predicate,
                        action_value, beneficiaries_json, participants_json,
                        condition_value, due_at, status, revision,
                        created_at, updated_at, drawer_projection_state,
                        kg_projection_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending')
                    """,
                    _record_values(record),
                )
            else:
                conn.execute(
                    """
                    UPDATE commitments
                    SET participants_json = ?, condition_value = ?, due_at = ?,
                        status = ?, revision = ?, updated_at = ?,
                        drawer_projection_state = 'pending',
                        kg_projection_state = 'pending'
                    WHERE commitment_id = ? AND memory_space_id = ?
                    """,
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
                f"{commitment_id}\x1f{intent.intent_id}".encode()
            ).hexdigest()[:32]
            conn.execute(
                """
                INSERT INTO commitment_revisions (
                    revision_id, commitment_id, intent_id, intent_hash,
                    source_event_id, authority, operation, previous_status,
                    status, raw_claim, snapshot_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    commitment_id,
                    intent.intent_id,
                    intent_hash,
                    intent.source_event_id,
                    intent.authority,
                    intent.operation_hint or "add",
                    previous_status,
                    status,
                    intent.raw_claim,
                    snapshot_json,
                    now,
                ),
            )
            revision_row = conn.execute(
                "SELECT * FROM commitment_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        return CommitmentApplyResult(
            commitment=record,
            revision=_revision_record(revision_row),
            commitment_created=created,
            revision_created=True,
        )

    def _mark_projected_sync(
        self,
        memory_space_id: str,
        commitment_id: str,
        revision: int,
        targets: set[str],
    ) -> None:
        columns = {
            "drawer": "drawer_projection_state",
            "kg": "kg_projection_state",
        }
        if not targets or not targets.issubset(columns):
            raise ValueError("commitment projection update requires known targets")
        assignments = ", ".join(
            f"{columns[target]} = 'projected'" for target in sorted(targets)
        )
        with self._connect() as conn:
            result = conn.execute(
                f"""
                UPDATE commitments SET {assignments}
                WHERE memory_space_id = ? AND commitment_id = ? AND revision = ?
                """,
                (memory_space_id, commitment_id, revision),
            )
            if result.rowcount != 1:
                raise CommitmentConflict(
                    "commitment revision changed before projection completed"
                )

    def _load_commitment(
        self, conn: sqlite3.Connection, commitment_id: str
    ) -> CommitmentRecord:
        row = conn.execute(
            "SELECT * FROM commitments WHERE commitment_id = ?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise LookupError("commitment not found")
        return _record(row)

    def _get_sync(
        self, memory_space_id: str, commitment_id: str
    ) -> CommitmentRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM commitments
                WHERE memory_space_id = ? AND commitment_id = ?
                """,
                (memory_space_id, commitment_id),
            ).fetchone()
        return _record(row) if row is not None else None

    def _list_current_page_sync(
        self, memory_space_id: str, include_terminal: bool, limit: int
    ) -> CommitmentListPage:
        bounded = max(1, min(int(limit), 200))
        with self._connect() as conn:
            conn.execute("BEGIN")
            if include_terminal:
                total = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM commitments WHERE memory_space_id = ?",
                        (memory_space_id,),
                    ).fetchone()[0]
                )
                rows = conn.execute(
                    """
                    SELECT * FROM commitments WHERE memory_space_id = ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (memory_space_id, bounded),
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in ACTIVE_COMMITMENT_STATUSES)
                params = (memory_space_id, *sorted(ACTIVE_COMMITMENT_STATUSES))
                total = int(
                    conn.execute(
                        f"""
                        SELECT COUNT(*) FROM commitments
                        WHERE memory_space_id = ? AND status IN ({placeholders})
                        """,
                        params,
                    ).fetchone()[0]
                )
                rows = conn.execute(
                    f"""
                    SELECT * FROM commitments
                    WHERE memory_space_id = ? AND status IN ({placeholders})
                    ORDER BY
                        CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                        julianday(due_at) ASC,
                        updated_at DESC,
                        commitment_id ASC
                    LIMIT ?
                    """,
                    (*params, bounded),
                ).fetchall()
        commitments = [_record(row) for row in rows]
        return CommitmentListPage(
            commitments=commitments,
            total=total,
            limit=bounded,
            truncated=total > len(commitments),
        )

    def _history_sync(
        self, memory_space_id: str, commitment_id: str, limit: int
    ) -> list[CommitmentRevisionRecord]:
        bounded = max(1, min(int(limit), 500))
        with self._connect() as conn:
            owner = conn.execute(
                """
                SELECT 1 FROM commitments
                WHERE memory_space_id = ? AND commitment_id = ?
                """,
                (memory_space_id, commitment_id),
            ).fetchone()
            if owner is None:
                return []
            rows = conn.execute(
                """
                SELECT * FROM commitment_revisions
                WHERE commitment_id = ?
                ORDER BY recorded_at DESC, revision_id DESC LIMIT ?
                """,
                (commitment_id, bounded),
            ).fetchall()
        return [_revision_record(row) for row in reversed(rows)]


def _intent_fields(intent: MemoryIntent) -> dict[str, Any]:
    if (
        intent.intent_type != "commitment"
        or not intent.subject
        or intent.predicate not in COMMITMENT_PREDICATES
        or not intent.object
    ):
        raise ValueError("commitment requires subject, commitment predicate and action")
    attributes = intent.attributes
    beneficiaries = _string_list(attributes.get("beneficiaries"))
    participants = _string_list(attributes.get("participants"))
    condition = _optional_text(attributes.get("condition"))
    due_at = _optional_text(attributes.get("due_at"))
    if due_at is not None:
        datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    return {
        "promisor": intent.subject,
        "predicate": intent.predicate,
        "action": intent.object,
        "beneficiaries": beneficiaries,
        "beneficiaries_provided": "beneficiaries" in attributes,
        "participants": participants,
        "condition": condition,
        "due_at": due_at,
    }


def _requested_status(intent: MemoryIntent, current: CommitmentStatus) -> CommitmentStatus:
    requested = intent.attributes.get("status")
    if requested is None:
        if intent.operation_hint == "confirm":
            return "confirmed"
        if intent.operation_hint == "invalidate":
            return "cancelled"
        return current
    if requested not in ACTIVE_COMMITMENT_STATUSES | TERMINAL_COMMITMENT_STATUSES:
        raise CommitmentConflict(f"unsupported commitment status: {requested}")
    return requested


def _validate_identity(
    row: sqlite3.Row, intent: MemoryIntent, fields: dict[str, Any]
) -> None:
    if str(row["memory_space_id"]) != intent.memory_space_id:
        raise CommitmentConflict("commitment belongs to another memory space")
    expected = (
        str(row["promisor"]),
        str(row["predicate"]),
        str(row["action_value"]),
        _json_list(row["beneficiaries_json"]),
    )
    actual = (
        fields["promisor"],
        fields["predicate"],
        fields["action"],
        fields["beneficiaries"],
    )
    if expected != actual:
        raise CommitmentConflict("commitment identity fields cannot be changed")


def _intent_hash(intent: MemoryIntent) -> str:
    payload = json.dumps(
        intent.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record(row: sqlite3.Row) -> CommitmentRecord:
    return CommitmentRecord(
        commitment_id=str(row["commitment_id"]),
        memory_space_id=str(row["memory_space_id"]),
        promisor=str(row["promisor"]),
        predicate=str(row["predicate"]),
        action=str(row["action_value"]),
        beneficiaries=_json_list(row["beneficiaries_json"]),
        participants=_json_list(row["participants_json"]),
        condition=row["condition_value"] or None,
        due_at=row["due_at"] or None,
        status=str(row["status"]),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        drawer_projection_state=str(row["drawer_projection_state"]),
        kg_projection_state=str(row["kg_projection_state"]),
    )


def _revision_record(row: sqlite3.Row) -> CommitmentRevisionRecord:
    snapshot = CommitmentRecord.model_validate_json(str(row["snapshot_json"]))
    return CommitmentRevisionRecord(
        revision_id=str(row["revision_id"]),
        commitment_id=str(row["commitment_id"]),
        intent_id=str(row["intent_id"]),
        source_event_id=str(row["source_event_id"]),
        authority=str(row["authority"]),
        operation=str(row["operation"]),
        previous_status=row["previous_status"] or None,
        status=str(row["status"]),
        raw_claim=str(row["raw_claim"]),
        snapshot=snapshot,
        recorded_at=str(row["recorded_at"]),
    )


def _record_values(record: CommitmentRecord) -> tuple[object, ...]:
    return (
        record.commitment_id,
        record.memory_space_id,
        record.promisor,
        record.predicate,
        record.action,
        json.dumps(record.beneficiaries, ensure_ascii=False),
        json.dumps(record.participants, ensure_ascii=False),
        record.condition,
        record.due_at,
        record.status,
        record.revision,
        record.created_at,
        record.updated_at,
    )


def _json_list(value: object) -> list[str]:
    if not value:
        return []
    parsed = json.loads(str(value))
    return [str(item) for item in parsed]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})


def _merge_values(current: list[str], incoming: list[str]) -> list[str]:
    return sorted(set(current) | set(incoming))


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("commitment text attributes must be strings")
    return value.strip() or None
