"""Realm-local SQLite commitment state and immutable revision history."""

from __future__ import annotations

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
from eidolon.memory.domain.commitment_decision import decide_commitment_apply
from eidolon.memory.infrastructure.ledger_sql import (
    COMMITMENT_COLUMNS,
    COMMITMENT_INSERT,
    COMMITMENT_REVISION_BY_ID,
    COMMITMENT_REVISION_BY_INTENT,
    COMMITMENT_REVISION_COLUMNS,
    COMMITMENT_REVISION_INSERT,
    COMMITMENT_REVISIONS_INDEX,
    COMMITMENT_REVISIONS_SCHEMA,
    COMMITMENT_SELECT_BY_ID,
    COMMITMENT_SELECT_ONE,
    COMMITMENT_UPDATE,
    COMMITMENTS_INDEX,
    COMMITMENTS_SCHEMA,
    SQLITE_MARKER,
    commitment_mark_projected,
    render,
)
from eidolon.memory.infrastructure.sqlite_writes import SerialisedSqliteWrites


def _sql(template: str) -> str:
    return render(template, SQLITE_MARKER)


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
            conn.execute(COMMITMENTS_SCHEMA)
            conn.execute(COMMITMENTS_INDEX)
            conn.execute(COMMITMENT_REVISIONS_SCHEMA)
            conn.execute(COMMITMENT_REVISIONS_INDEX)

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
        """Read, decide, write — one transaction, decision in a pure function.

        The state machine and merge rules live in
        :func:`decide_commitment_apply`, which the PostgreSQL implementation calls
        too. Keeping them out of here is what stops the two storages from holding
        two copies of one state machine.
        """

        fields = _intent_fields(intent)
        now = datetime.now(UTC).isoformat()
        intent_hash = _intent_hash(intent)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                _sql(COMMITMENT_REVISION_BY_INTENT), (intent.intent_id,)
            ).fetchone()
            if replay is not None:
                stored = _revision_from_row(replay)
                if stored.intent_hash != intent_hash:
                    raise CommitmentConflict(
                        "intent id reused with different commitment payload"
                    )
                row = conn.execute(
                    _sql(COMMITMENT_SELECT_BY_ID), (stored.commitment_id,)
                ).fetchone()
                assert row is not None
                return CommitmentApplyResult(
                    commitment=_record(row),
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
            existing_row = conn.execute(
                _sql(COMMITMENT_SELECT_BY_ID), (commitment_id,)
            ).fetchone()
            existing = _record(existing_row) if existing_row is not None else None
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
                conn.execute(_sql(COMMITMENT_INSERT), _record_values(record))
            else:
                conn.execute(
                    _sql(COMMITMENT_UPDATE),
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
            conn.execute(
                _sql(COMMITMENT_REVISION_INSERT),
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
            revision_row = conn.execute(
                _sql(COMMITMENT_REVISION_BY_ID), (revision_id,)
            ).fetchone()
        return CommitmentApplyResult(
            commitment=record,
            revision=_revision_record(revision_row),
            commitment_created=decision.created,
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
        with self._connect() as conn:
            result = conn.execute(
                commitment_mark_projected(SQLITE_MARKER, [columns[t] for t in targets]),
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
                _sql(COMMITMENT_SELECT_ONE), (memory_space_id, commitment_id)
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
    stored: CommitmentRecord, intent: MemoryIntent, fields: dict[str, Any]
) -> None:
    """Refuse a revision that would change what the commitment *is*.

    Takes a record rather than a row, so the PostgreSQL implementation can pass
    its own — this is a rule about commitments, not about SQLite.
    """

    if stored.memory_space_id != intent.memory_space_id:
        raise CommitmentConflict("commitment belongs to another memory space")
    expected = (
        stored.promisor,
        stored.predicate,
        stored.action,
        stored.beneficiaries,
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


def _record(row) -> CommitmentRecord:
    """Read positionally, in the order the shared statements select."""
    row = dict(zip(COMMITMENT_COLUMNS, row, strict=True))
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


class _StoredRevision:
    """A revision row plus the intent hash, which the record type does not carry."""

    __slots__ = ("record", "intent_hash", "commitment_id")

    def __init__(self, record, intent_hash: str, commitment_id: str) -> None:
        self.record = record
        self.intent_hash = intent_hash
        self.commitment_id = commitment_id


def _revision_from_row(row) -> _StoredRevision:
    values = dict(zip(COMMITMENT_REVISION_COLUMNS, row, strict=True))
    return _StoredRevision(
        record=_revision_record(row),
        intent_hash=str(values["intent_hash"]),
        commitment_id=str(values["commitment_id"]),
    )


def _revision_record(row) -> CommitmentRevisionRecord:
    row = dict(zip(COMMITMENT_REVISION_COLUMNS, row, strict=True))
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
