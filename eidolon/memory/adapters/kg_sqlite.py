"""The knowledge graph in a file, for deployments that keep memories on disk.

Owns its schema rather than borrowing MemPalace's. That buys two things beyond
independence: audience and sensitivity become columns, so filtering happens in
the query rather than over the results; and timestamps are normalised on the way
in, so the interval test is plain SQL instead of a dialect trick to widen
date-only values at comparison time.

Serialisation is the caller's lock, shared with the vector store. A turn writes
to both, and one critical section covering the pair is easier to reason about
than an ordering between two.

That lock is a readers-writer lock, not a mutex, and the distinction is
load-bearing here: ``recall_with_kg_fusion`` starts a graph lookup alongside the
vector search on a 50ms voice budget, and under a mutex the lookup spent that
budget waiting for the search rather than querying the graph. Reads below take the
reader side; the four methods that mutate take the writer side.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eidolon_memory_contracts import SENSITIVE_PREDICATES

from eidolon.memory.adapters.kg_sql import (
    JOIN_ENTITIES,
    ORDER_BY_RELEVANCE,
    RANKED_SUBJECT_COLUMNS,
    SCHEMA_STATEMENTS,
    SELECT_COLUMNS,
    SUBJECT_RANK,
    VALID_AT,
    audience_filter,
    name_appears_in,
)
from eidolon.memory.domain.kg import KgTripleRecord
from eidolon.memory.domain.space_lock import SpaceLock
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MARKER = "?"


def now_iso() -> str:
    """UTC, second resolution, ``Z`` suffix.

    One canonical form everywhere, because these are compared as strings.
    """

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_temporal(value: str | None) -> str | None:
    """Normalise a timestamp so string comparison behaves like time comparison.

    A date-only value is widened to midnight UTC here rather than at every
    comparison. MemPalace's schema kept them narrow and worked around it in each
    query with a length check; doing it once on write means the interval
    predicate is ordinary SQL and behaves the same in any store.

    An unparseable value is passed through rather than rejected — callers upstream
    include LLM output, and losing a statement to a malformed date is worse than
    storing a timestamp we cannot order.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _DATE_ONLY.match(text):
        return f"{text}T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def entity_id_for(name: str) -> str:
    """Key an entity by a slug of its name.

    Names arrive from an LLM with inconsistent casing and spacing, so the slug is
    what makes "My Dad" and "my dad" the same entity.
    """

    return (name or "").strip().lower().replace(" ", "_").replace("'", "")


def statement_id_for(
    subject_id: str, predicate: str, object_id: str, valid_from: str, recorded_at: str
) -> str:
    """Derive a statement's id from its content and when it started.

    Deterministic, so the same statement recorded twice in one instant collides
    rather than duplicating. ``recorded_at`` is included because the same triple
    may legitimately hold over more than one interval — someone moves away and
    back — and those are different statements.
    """

    payload = "\x1f".join((subject_id, predicate, object_id, valid_from, recorded_at))
    return f"stmt_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def predicate_is_sensitive(predicate: str) -> bool:
    return predicate in SENSITIVE_PREDICATES


class SqliteKnowledgeGraph:
    """A space's graph in one SQLite file."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        space_id: str,
        lock: SpaceLock,
    ) -> None:
        self._path = Path(db_path)
        self._space_id = space_id
        self._lock = lock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialise()

    @property
    def lock(self) -> SpaceLock:
        return self._lock

    def _initialise(self) -> None:
        with self._conn:
            # WAL so a reader is never blocked by the turn currently writing.
            #
            # That was true of the file and false of the code until 2026-08-05: the
            # shared lock was an exclusive mutex, so every read here waited for the
            # turn anyway and WAL bought nothing. Reads now take the reader side,
            # which is what makes this pragma mean what it says.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            for statement in SCHEMA_STATEMENTS:
                self._conn.execute(statement)

    # ── writes ──────────────────────────────────────────────────────────────

    async def add_triple(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        audience: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        confidence: float = 1.0,
        source_turn_id: str | None = None,
        adapter_name: str | None = None,
        sensitive: bool | None = None,
    ) -> str:
        async with self._lock.writer():
            return await asyncio.to_thread(
                self._add_triple_sync,
                subject,
                predicate,
                object,
                audience,
                valid_from,
                valid_to,
                confidence,
                source_turn_id,
                adapter_name,
                sensitive,
            )

    def _add_triple_sync(
        self,
        subject: str,
        predicate: str,
        object: str,
        audience: str,
        valid_from: str | None,
        valid_to: str | None,
        confidence: float,
        source_turn_id: str | None,
        adapter_name: str | None,
        sensitive: bool | None,
    ) -> str:
        subject_id = entity_id_for(subject)
        object_id = entity_id_for(object)
        started = canonical_temporal(valid_from) or now_iso()
        ended = canonical_temporal(valid_to)

        # Replaying a turn must not duplicate, and must not undo an invalidation
        # that happened after it — so this check comes before the validity one.
        if source_turn_id:
            existing = self._conn.execute(
                """
                SELECT statement_id FROM kg_statements
                WHERE space_id = ? AND source_turn_id = ?
                  AND subject_id = ? AND predicate = ? AND object_id = ?
                LIMIT 1
                """,
                (self._space_id, source_turn_id, subject_id, predicate, object_id),
            ).fetchone()
            if existing is not None:
                return existing["statement_id"]

        # An identical statement that is still open is the same statement.
        still_valid = self._conn.execute(
            """
            SELECT statement_id FROM kg_statements
            WHERE space_id = ? AND subject_id = ? AND predicate = ? AND object_id = ?
              AND valid_to IS NULL
            LIMIT 1
            """,
            (self._space_id, subject_id, predicate, object_id),
        ).fetchone()
        if still_valid is not None:
            return still_valid["statement_id"]

        recorded = now_iso()
        statement_id = statement_id_for(subject_id, predicate, object_id, started, recorded)
        is_sensitive = predicate_is_sensitive(predicate) if sensitive is None else sensitive

        with self._conn:
            self._upsert_entity(subject_id, subject, recorded)
            self._upsert_entity(object_id, object, recorded)
            self._conn.execute(
                """
                INSERT OR IGNORE INTO kg_statements (
                    space_id, statement_id, subject_id, predicate, object_id,
                    audience, sensitive, valid_from, valid_to, recorded_at,
                    confidence, source_turn_id, adapter_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._space_id,
                    statement_id,
                    subject_id,
                    predicate,
                    object_id,
                    audience,
                    1 if is_sensitive else 0,
                    started,
                    ended,
                    recorded,
                    confidence,
                    source_turn_id,
                    adapter_name,
                ),
            )
        return statement_id

    def _upsert_entity(self, entity_id: str, name: str, recorded: str) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO kg_entities (
                space_id, entity_id, name, entity_type, properties, created_at
            ) VALUES (?, ?, ?, 'unknown', '{}', ?)
            """,
            (self._space_id, entity_id, (name or "").strip(), recorded),
        )

    async def invalidate(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        ended: str | None = None,
    ) -> int:
        async with self._lock.writer():
            return await asyncio.to_thread(
                self._invalidate_sync, subject, predicate, object, ended
            )

    def _invalidate_sync(
        self, subject: str, predicate: str, object: str, ended: str | None
    ) -> int:
        ended_at = canonical_temporal(ended) or now_iso()
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE kg_statements SET valid_to = ?
                WHERE space_id = ? AND subject_id = ? AND predicate = ? AND object_id = ?
                  AND valid_to IS NULL
                """,
                (ended_at, self._space_id, entity_id_for(subject), predicate,
                 entity_id_for(object)),
            )
        return cursor.rowcount or 0

    async def supersede(
        self,
        *,
        subject: str,
        predicate: str,
        old_object: str,
        new_object: str,
        audience: str,
        changed_at: str | None = None,
        confidence: float = 1.0,
        source_turn_id: str | None = None,
    ) -> str:
        """End the old statement and start the new one at the same instant.

        Both happen inside one lock hold and one transaction, so no reader sees a
        moment where the fact is absent.
        """

        boundary = canonical_temporal(changed_at) or now_iso()
        async with self._lock.writer():
            return await asyncio.to_thread(
                self._supersede_sync,
                subject,
                predicate,
                old_object,
                new_object,
                audience,
                boundary,
                confidence,
                source_turn_id,
            )

    def _supersede_sync(
        self,
        subject: str,
        predicate: str,
        old_object: str,
        new_object: str,
        audience: str,
        boundary: str,
        confidence: float,
        source_turn_id: str | None,
    ) -> str:
        self._invalidate_sync(subject, predicate, old_object, boundary)
        return self._add_triple_sync(
            subject,
            predicate,
            new_object,
            audience,
            boundary,
            None,
            confidence,
            source_turn_id,
            None,
            None,
        )

    async def record_entity_mention(
        self,
        *,
        entity_id: str,
        alias: str,
        source: str,
        confidence: float = 0.85,
    ) -> None:
        async with self._lock.writer():
            await asyncio.to_thread(
                self._record_mention_sync, entity_id, alias, source, confidence
            )

    def _record_mention_sync(
        self, entity_id: str, alias: str, source: str, confidence: float
    ) -> None:
        normalised = (alias or "").strip().lower()
        if not normalised or not entity_id:
            return
        mention_id = f"{entity_id}:{normalised}"
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO kg_entity_mentions (
                    space_id, mention_id, entity_id, alias, source, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (self._space_id, mention_id, entity_id, normalised, source,
                 confidence, now_iso()),
            )

    # ── reads ───────────────────────────────────────────────────────────────

    async def query_entity(
        self,
        name: str,
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        direction: str = "outgoing",
        include_sensitive: bool = False,
    ) -> list[KgTripleRecord]:
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError(
                f"direction must be outgoing|incoming|both, got {direction!r}"
            )
        moment = canonical_temporal(as_of) or now_iso()
        async with self._lock.reader():
            return await asyncio.to_thread(
                self._query_entity_sync, name, audiences, moment, direction,
                include_sensitive,
            )

    def _query_entity_sync(
        self,
        name: str,
        audiences: tuple[str, ...],
        moment: str,
        direction: str,
        include_sensitive: bool,
    ) -> list[KgTripleRecord]:
        if not audiences:
            return []
        entity = entity_id_for(name)
        columns = "s.subject_id = ?" if direction == "outgoing" else "s.object_id = ?"
        if direction == "both":
            columns = "(s.subject_id = ? OR s.object_id = ?)"
        params: list[Any] = [self._space_id]
        params.extend([entity, entity] if direction == "both" else [entity])
        params.append(moment)
        params.append(moment)
        params.extend(audiences)
        sql = (
            f"SELECT {SELECT_COLUMNS} {JOIN_ENTITIES} "
            f"WHERE s.space_id = ? AND {columns} "
            f"AND {VALID_AT.format(p=_MARKER)} "
            f"AND {audience_filter(len(audiences), _MARKER)} "
            f"{self._sensitive_clause(include_sensitive)} "
            f"{ORDER_BY_RELEVANCE}"
        )
        return [_to_record(row) for row in self._conn.execute(sql, params)]

    async def query_subjects(
        self,
        names: list[str],
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        include_sensitive: bool = False,
        limit_per_subject: int = 8,
    ) -> list[KgTripleRecord]:
        moment = canonical_temporal(as_of) or now_iso()
        async with self._lock.reader():
            return await asyncio.to_thread(
                self._query_subjects_sync, names, audiences, moment,
                include_sensitive, limit_per_subject,
            )

    def _query_subjects_sync(
        self,
        names: list[str],
        audiences: tuple[str, ...],
        moment: str,
        include_sensitive: bool,
        limit_per_subject: int,
    ) -> list[KgTripleRecord]:
        if not names or not audiences or limit_per_subject <= 0:
            return []
        wanted = [entity_id_for(name) for name in names if name and name.strip()]
        if not wanted:
            return []

        # Ranked within each subject, so every subject asked about gets its own
        # allowance. A single overall LIMIT would let one well-connected entity
        # spend the whole budget and leave the others unrepresented — which is
        # what a plain ordered query plus per-subject bucketing in Python does.
        #
        # One statement rather than one query per subject: locally the difference
        # is nothing, but against a database each extra query is another round
        # trip on a path with a latency budget.
        placeholders = ", ".join(_MARKER for _ in wanted)
        sql = (
            f"SELECT {RANKED_SUBJECT_COLUMNS} FROM ("
            f"  SELECT {SELECT_COLUMNS}, {SUBJECT_RANK} {JOIN_ENTITIES} "
            f"  WHERE s.space_id = ? AND s.subject_id IN ({placeholders}) "
            f"  AND {VALID_AT.format(p=_MARKER)} "
            f"  AND {audience_filter(len(audiences), _MARKER)} "
            f"  {self._sensitive_clause(include_sensitive)}"
            f") ranked WHERE ranked.subject_rank <= ?"
        )
        params: list[Any] = [
            self._space_id, *wanted, moment, moment, *audiences, limit_per_subject
        ]
        return [_to_record(row) for row in self._conn.execute(sql, params)]

    async def query_entity_combined(
        self,
        names: list[str],
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        include_sensitive: bool = False,
        limit_per_entity: int = 8,
    ) -> list[KgTripleRecord]:
        collected: list[KgTripleRecord] = []
        seen: set[str] = set()
        for name in names:
            rows = await self.query_entity(
                name,
                audiences=audiences,
                as_of=as_of,
                direction="both",
                include_sensitive=include_sensitive,
            )
            for record in rows[:limit_per_entity]:
                if record.id not in seen:
                    seen.add(record.id)
                    collected.append(record)
        return collected

    async def timeline(
        self,
        entity_name: str | None = None,
        *,
        audiences: tuple[str, ...],
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        include_sensitive: bool = False,
    ) -> list[KgTripleRecord]:
        async with self._lock.reader():
            return await asyncio.to_thread(
                self._timeline_sync, entity_name, audiences, since, until, limit,
                include_sensitive,
            )

    def _timeline_sync(
        self,
        entity_name: str | None,
        audiences: tuple[str, ...],
        since: str | None,
        until: str | None,
        limit: int,
        include_sensitive: bool,
    ) -> list[KgTripleRecord]:
        if not audiences:
            return []
        clauses = ["s.space_id = ?"]
        params: list[Any] = [self._space_id]
        if entity_name:
            entity = entity_id_for(entity_name)
            clauses.append("(s.subject_id = ? OR s.object_id = ?)")
            params.extend([entity, entity])
        start = canonical_temporal(since)
        if start:
            clauses.append("(s.valid_from IS NULL OR s.valid_from >= ?)")
            params.append(start)
        end = canonical_temporal(until)
        if end:
            clauses.append("(s.valid_from IS NULL OR s.valid_from <= ?)")
            params.append(end)
        clauses.append(audience_filter(len(audiences), _MARKER))
        params.extend(audiences)
        sql = (
            f"SELECT {SELECT_COLUMNS} {JOIN_ENTITIES} "
            f"WHERE {' AND '.join(clauses)} "
            f"{self._sensitive_clause(include_sensitive)} "
            "ORDER BY s.valid_from DESC, s.recorded_at DESC LIMIT ?"
        )
        params.append(max(1, limit))
        return [_to_record(row) for row in self._conn.execute(sql, params)]

    async def match_entities_for_query(self, query: str, *, cap: int) -> list[str]:
        if cap <= 0:
            return []
        async with self._lock.reader():
            return await asyncio.to_thread(self._match_entities_sync, query, cap)

    def _match_entities_sync(self, query: str, cap: int) -> list[str]:
        """Find entities a phrase might be about.

        Bridges two naming conventions that do not meet on their own. The steward
        writes type-prefixed names to disambiguate — ``pet:铁锤``, ``place:北京``,
        ``mother:张丽`` — while a person asks about "铁锤". So a canonical name
        matches either whole or by the tail after its prefix.

        Longest first, within each strategy, so ``mother:张丽`` beats a bare
        ``mother`` when both could fire. Canonical names are tried before
        aliases, and an entity already matched canonically is not re-emitted via
        an alias.

        Best effort by design: a miss costs the graph's contribution to one
        recall, which the vector result already covers.
        """

        text = (query or "").strip()
        if not text or cap <= 0:
            return []

        found: list[str] = []
        seen: set[str] = set()

        names = [
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM kg_entities WHERE space_id = ?", (self._space_id,)
            )
        ]
        for name in sorted(set(names), key=lambda value: -len(value)):
            if name in seen:
                continue
            if name_appears_in(name, text):
                found.append(name)
                seen.add(name)
                if len(found) >= cap:
                    return found

        aliases = [
            (row["alias"] or "", row["name"] or "")
            for row in self._conn.execute(
                """
                SELECT m.alias AS alias, e.name AS name FROM kg_entity_mentions m
                JOIN kg_entities e ON e.space_id = m.space_id AND e.entity_id = m.entity_id
                WHERE m.space_id = ?
                """,
                (self._space_id,),
            )
        ]
        # Longest alias first, so "我老婆" wins over the substring "老婆".
        lowered = text.lower()
        for alias, name in sorted(aliases, key=lambda pair: -len(pair[0])):
            if not alias or name in seen:
                continue
            if alias in lowered:
                found.append(name)
                seen.add(name)
                if len(found) >= cap:
                    return found
        return found

    async def known_audiences(self) -> list[str]:
        """Which audiences this space's graph actually contains.

        For operator tools that inspect a space in full. Enumerating is
        deliberate: a wildcard "any audience" token would be a way past the
        filter that keeps one companion's statements out of another's recall.
        """

        async with self._lock.reader():
            return await asyncio.to_thread(self._known_audiences_sync)

    def _known_audiences_sync(self) -> list[str]:
        return [
            row["audience"]
            for row in self._conn.execute(
                "SELECT DISTINCT audience FROM kg_statements WHERE space_id = ?",
                (self._space_id,),
            )
        ]

    async def list_entity_names(self) -> list[str]:
        async with self._lock.reader():
            return await asyncio.to_thread(self._list_entity_names_sync)

    def _list_entity_names_sync(self) -> list[str]:
        return [
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM kg_entities WHERE space_id = ? ORDER BY name",
                (self._space_id,),
            )
        ]

    async def stats(self) -> dict[str, Any]:
        async with self._lock.reader():
            return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM kg_entities WHERE space_id = ?) AS entities,
                (SELECT COUNT(*) FROM kg_statements WHERE space_id = ?) AS total,
                (SELECT COUNT(*) FROM kg_statements
                   WHERE space_id = ? AND valid_to IS NULL) AS active,
                (SELECT COUNT(*) FROM kg_entity_mentions WHERE space_id = ?) AS mentions
            """,
            (self._space_id,) * 4,
        ).fetchone()
        return {
            "entities": row["entities"],
            "triples_total": row["total"],
            "triples_active": row["active"],
            "triples_invalidated": row["total"] - row["active"],
            "mentions": row["mentions"],
        }

    # ── idempotency probes ──────────────────────────────────────────────────

    async def has_triple(self, triple_id: str) -> bool:
        async with self._lock.reader():
            return await asyncio.to_thread(self._has_triple_sync, triple_id)

    def _has_triple_sync(self, triple_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM kg_statements WHERE space_id = ? AND statement_id = ?",
                (self._space_id, triple_id),
            ).fetchone()
            is not None
        )

    async def find_pending_triple_id(
        self, source_turn_id: str, subject: str, predicate: str, object: str
    ) -> str | None:
        async with self._lock.reader():
            return await asyncio.to_thread(
                self._find_pending_sync, source_turn_id, subject, predicate, object
            )

    def _find_pending_sync(
        self, source_turn_id: str, subject: str, predicate: str, object: str
    ) -> str | None:
        row = self._conn.execute(
            """
            SELECT statement_id FROM kg_statements
            WHERE space_id = ? AND source_turn_id = ?
              AND subject_id = ? AND predicate = ? AND object_id = ?
            LIMIT 1
            """,
            (self._space_id, source_turn_id, entity_id_for(subject), predicate,
             entity_id_for(object)),
        ).fetchone()
        return row["statement_id"] if row else None

    async def find_invalidation_applied(
        self, subject: str, predicate: str, object: str, ended_at_or_before: str
    ) -> bool:
        async with self._lock.reader():
            return await asyncio.to_thread(
                self._find_invalidation_sync, subject, predicate, object,
                ended_at_or_before,
            )

    def _find_invalidation_sync(
        self, subject: str, predicate: str, object: str, ended_at_or_before: str
    ) -> bool:
        boundary = canonical_temporal(ended_at_or_before) or now_iso()
        row = self._conn.execute(
            """
            SELECT 1 FROM kg_statements
            WHERE space_id = ? AND subject_id = ? AND predicate = ? AND object_id = ?
              AND valid_to IS NOT NULL AND valid_to <= ?
            LIMIT 1
            """,
            (self._space_id, entity_id_for(subject), predicate,
             entity_id_for(object), boundary),
        ).fetchone()
        return row is not None

    @staticmethod
    def _sensitive_clause(include_sensitive: bool) -> str:
        """Filter sensitivity in the query rather than over the results.

        A column, not a predicate-name check in Python: the store never hands
        back a health statement the caller did not ask for, so there is no window
        in which one has been read and not yet dropped.
        """

        return "" if include_sensitive else "AND s.sensitive = 0"

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as exc:  # noqa: BLE001 - closing twice is not an error
            log.warning("kg_sqlite_close_failed", error=str(exc))


def _to_record(row: sqlite3.Row) -> KgTripleRecord:
    return KgTripleRecord(
        id=row[0],
        subject=row[1],
        predicate=row[2],
        object=row[3],
        valid_from=row[4],
        valid_to=row[5],
        confidence=row[6],
        source_turn_id=row[7],
        adapter_name=row[8],
    )


__all__ = [
    "SqliteKnowledgeGraph",
    "canonical_temporal",
    "entity_id_for",
    "now_iso",
    "predicate_is_sensitive",
    "statement_id_for",
]


