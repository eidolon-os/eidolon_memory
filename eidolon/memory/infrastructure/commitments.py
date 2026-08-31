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
    CommitmentForgetPlan,
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
    COMMITMENT_PRIVACY_SCHEMA,
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
    commitment_mark_projected,
)
from eidolon.memory.infrastructure.sqlite_writes import SerialisedSqliteWrites


def _sql(template: str) -> str:
    return template


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
        conn.execute("PRAGMA secure_delete=ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(COMMITMENTS_SCHEMA)
            conn.execute(COMMITMENTS_INDEX)
            conn.execute(COMMITMENT_REVISIONS_SCHEMA)
            conn.execute(COMMITMENT_REVISIONS_INDEX)
            conn.execute(COMMITMENT_PRIVACY_SCHEMA)

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

    async def list_for_privacy(
        self, memory_space_id: str, *, limit: int, offset: int
    ) -> list[CommitmentRecord]:
        return await self._read(
            self._list_for_privacy_sync, memory_space_id, limit, offset
        )

    async def begin_forget(
        self,
        memory_space_id: str,
        commitment_ids: list[str],
        *,
        hard: bool,
    ) -> list[CommitmentForgetPlan]:
        return await self._write(
            self._begin_forget_sync, memory_space_id, commitment_ids, hard
        )

    async def mark_forget_projected(
        self,
        memory_space_id: str,
        commitment_ids: list[str],
        *,
        targets: set[str],
    ) -> None:
        await self._write(
            self._mark_forget_projected_sync,
            memory_space_id,
            commitment_ids,
            targets,
        )

    async def finalize_forget(
        self, memory_space_id: str, commitment_ids: list[str]
    ) -> None:
        await self._write(self._finalize_forget_sync, memory_space_id, commitment_ids)

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
        fields["identity_id"] = commitment_identity(
            intent.memory_space_id,
            fields["promisor"],
            fields["predicate"],
            fields["action"],
            fields["beneficiaries"],
        )
        commitment_id = intent.target_id or fields["identity_id"]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            forgotten = conn.execute(
                """
                SELECT 1 FROM commitment_privacy
                WHERE memory_space_id = ? AND commitment_id = ?
                """,
                (intent.memory_space_id, commitment_id),
            ).fetchone()
            if forgotten is not None:
                raise CommitmentConflict("forgotten commitment cannot be replayed")
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
                commitment_mark_projected([columns[t] for t in targets]),
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
                _sql(COMMITMENT_SELECT_ONE)
                + """ AND NOT EXISTS (
                    SELECT 1 FROM commitment_privacy p
                    WHERE p.memory_space_id = commitments.memory_space_id
                      AND p.commitment_id = commitments.commitment_id
                )""",
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
                        """SELECT COUNT(*) FROM commitments
                        WHERE memory_space_id = ? AND NOT EXISTS (
                            SELECT 1 FROM commitment_privacy p
                            WHERE p.memory_space_id = commitments.memory_space_id
                              AND p.commitment_id = commitments.commitment_id
                        )""",
                        (memory_space_id,),
                    ).fetchone()[0]
                )
                rows = conn.execute(
                    """
                    SELECT * FROM commitments WHERE memory_space_id = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM commitment_privacy p
                        WHERE p.memory_space_id = commitments.memory_space_id
                          AND p.commitment_id = commitments.commitment_id
                    )
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
                        AND NOT EXISTS (
                            SELECT 1 FROM commitment_privacy p
                            WHERE p.memory_space_id = commitments.memory_space_id
                              AND p.commitment_id = commitments.commitment_id
                        )
                        """,
                        params,
                    ).fetchone()[0]
                )
                rows = conn.execute(
                    f"""
                    SELECT * FROM commitments
                    WHERE memory_space_id = ? AND status IN ({placeholders})
                    AND NOT EXISTS (
                        SELECT 1 FROM commitment_privacy p
                        WHERE p.memory_space_id = commitments.memory_space_id
                          AND p.commitment_id = commitments.commitment_id
                    )
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
                AND NOT EXISTS (
                    SELECT 1 FROM commitment_privacy p
                    WHERE p.memory_space_id = commitments.memory_space_id
                      AND p.commitment_id = commitments.commitment_id
                )
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

    def _list_for_privacy_sync(
        self, memory_space_id: str, limit: int, offset: int
    ) -> list[CommitmentRecord]:
        bounded = max(1, min(int(limit), 5000))
        start = max(0, int(offset))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT commitments.* FROM commitments
                LEFT JOIN commitment_privacy p
                  ON p.memory_space_id = commitments.memory_space_id
                 AND p.commitment_id = commitments.commitment_id
                WHERE commitments.memory_space_id = ?
                  AND (p.commitment_id IS NULL OR p.action = 'archive')
                ORDER BY commitments.commitment_id LIMIT ? OFFSET ?
                """,
                (memory_space_id, bounded, start),
            ).fetchall()
        return [_record(row) for row in rows]

    def _begin_forget_sync(
        self, memory_space_id: str, commitment_ids: list[str], hard: bool
    ) -> list[CommitmentForgetPlan]:
        wanted = list(dict.fromkeys(value.strip() for value in commitment_ids if value.strip()))
        if not wanted:
            return []
        now = datetime.now(UTC).isoformat()
        plans: list[CommitmentForgetPlan] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for commitment_id in wanted:
                privacy = conn.execute(
                    """SELECT action, revision_count FROM commitment_privacy
                    WHERE memory_space_id = ? AND commitment_id = ?""",
                    (memory_space_id, commitment_id),
                ).fetchone()
                if privacy is None:
                    row = conn.execute(
                        """SELECT revision FROM commitments
                        WHERE memory_space_id = ? AND commitment_id = ?""",
                        (memory_space_id, commitment_id),
                    ).fetchone()
                    if row is None:
                        raise LookupError("commitment forget target not found")
                    revision_count = int(row[0])
                    conn.execute(
                        """INSERT INTO commitment_privacy(
                            memory_space_id, commitment_id, action, revision_count,
                            forgotten_at
                        ) VALUES (?, ?, ?, ?, ?)""",
                        (
                            memory_space_id,
                            commitment_id,
                            "delete" if hard else "archive",
                            revision_count,
                            now,
                        ),
                    )
                else:
                    revision_count = int(privacy[1])
                    if hard and str(privacy[0]) != "delete":
                        conn.execute(
                            """UPDATE commitment_privacy SET action = 'delete'
                            WHERE memory_space_id = ? AND commitment_id = ?""",
                            (memory_space_id, commitment_id),
                        )
                source_rows = conn.execute(
                    """
                    SELECT DISTINCT revisions.source_event_id
                    FROM commitment_revisions AS revisions
                    JOIN commitments AS commitment
                      ON commitment.commitment_id = revisions.commitment_id
                    WHERE commitment.memory_space_id = ?
                      AND revisions.commitment_id = ?
                    ORDER BY revisions.source_event_id
                    """,
                    (memory_space_id, commitment_id),
                ).fetchall()
                plans.append(
                    CommitmentForgetPlan(
                        memory_space_id=memory_space_id,
                        commitment_id=commitment_id,
                        revision_count=revision_count,
                        source_event_ids=[str(row[0]) for row in source_rows],
                        hard=hard or (privacy is not None and str(privacy[0]) == "delete"),
                    )
                )
        return plans

    def _mark_forget_projected_sync(
        self,
        memory_space_id: str,
        commitment_ids: list[str],
        targets: set[str],
    ) -> None:
        columns = {"drawer": "drawer_projection_state", "kg": "kg_projection_state"}
        if not targets or not targets.issubset(columns):
            raise ValueError("commitment privacy projection requires known targets")
        wanted = list(dict.fromkeys(commitment_ids))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for commitment_id in wanted:
                assignments = ", ".join(
                    f"{columns[target]} = 'projected'" for target in sorted(targets)
                )
                result = conn.execute(
                    f"""UPDATE commitment_privacy SET {assignments}
                    WHERE memory_space_id = ? AND commitment_id = ?""",
                    (memory_space_id, commitment_id),
                )
                if result.rowcount != 1:
                    raise LookupError("commitment privacy tombstone not found")

    def _finalize_forget_sync(
        self, memory_space_id: str, commitment_ids: list[str]
    ) -> None:
        wanted = list(dict.fromkeys(commitment_ids))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for commitment_id in wanted:
                row = conn.execute(
                    """SELECT action, drawer_projection_state, kg_projection_state
                    FROM commitment_privacy
                    WHERE memory_space_id = ? AND commitment_id = ?""",
                    (memory_space_id, commitment_id),
                ).fetchone()
                if row is None:
                    raise LookupError("commitment privacy tombstone not found")
                if tuple(row) != ("delete", "projected", "projected"):
                    continue
                conn.execute(
                    "DELETE FROM commitment_revisions WHERE commitment_id = ?",
                    (commitment_id,),
                )
                conn.execute(
                    """DELETE FROM commitments
                    WHERE memory_space_id = ? AND commitment_id = ?""",
                    (memory_space_id, commitment_id),
                )
        with self._connect() as conn:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise RuntimeError("commitment privacy checkpoint remained busy")


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
