"""SQLite-backed durable Extraction Decision store."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from eidolon_memory_contracts import MemoryIntent

from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
)
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.sqlite_writes import SerialisedSqliteWrites


class ExtractionDecisionLedger(SerialisedSqliteWrites):
    """Persist validated steward output before Chroma/KG projection.

    This ledger is a decision source, not a second memory projection. It never
    writes Palace or KG state and therefore does not acquire the Realm lock.
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
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS extraction_decisions (
                    memory_space_id TEXT NOT NULL,
                    source_turn_id TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    intents_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (memory_space_id, source_turn_id, extractor_version)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(extraction_decisions)")
            }
            if "intents_json" not in columns:
                conn.execute(
                    "ALTER TABLE extraction_decisions "
                    "ADD COLUMN intents_json TEXT NOT NULL DEFAULT '[]'"
                )

    async def get(
        self,
        memory_space_id: str,
        source_turn_id: str,
        extractor_version: str,
    ) -> ExtractionDecisionRecord | None:
        return await self._read(
            self._get_sync,
            memory_space_id,
            source_turn_id,
            extractor_version,
        )

    async def put_if_absent(
        self,
        record: ExtractionDecisionRecord,
    ) -> ExtractionDecisionRecord:
        return await self._write(self._put_if_absent_sync, record)

    def _get_sync(
        self,
        memory_space_id: str,
        source_turn_id: str,
        extractor_version: str,
    ) -> ExtractionDecisionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM extraction_decisions
                WHERE memory_space_id = ? AND source_turn_id = ? AND extractor_version = ?
                """,
                (memory_space_id, source_turn_id, extractor_version),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def _put_if_absent_sync(
        self,
        record: ExtractionDecisionRecord,
    ) -> ExtractionDecisionRecord:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM extraction_decisions
                WHERE memory_space_id = ? AND source_turn_id = ? AND extractor_version = ?
                """,
                (
                    record.memory_space_id,
                    record.source_turn_id,
                    record.extractor_version,
                ),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO extraction_decisions (
                        memory_space_id, source_turn_id, extractor_version,
                        input_hash, decision_json, intents_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
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
                        record.created_at.isoformat(),
                    ),
                )
                return record

            stored = self._from_row(row)
            if stored.input_hash != record.input_hash:
                raise ExtractionDecisionConflict(
                    "extraction identity reused with different validated turn input"
                )
            return stored

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ExtractionDecisionRecord:
        return ExtractionDecisionRecord(
            memory_space_id=str(row["memory_space_id"]),
            source_turn_id=str(row["source_turn_id"]),
            extractor_version=str(row["extractor_version"]),
            input_hash=str(row["input_hash"]),
            decision=StewardDecision.model_validate_json(str(row["decision_json"])),
            intents=[
                MemoryIntent.model_validate(item)
                for item in json.loads(str(row["intents_json"]))
            ],
            created_at=str(row["created_at"]),
        )
