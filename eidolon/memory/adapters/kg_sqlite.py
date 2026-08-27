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
import json
import os
import re
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eidolon_memory_contracts import OWNER_AUDIENCE, SENSITIVE_PREDICATES, validate_audience

from eidolon.memory.adapters.kg_sql import (
    JOIN_ENTITIES,
    ORDER_BY_RELEVANCE,
    SCHEMA_STATEMENTS,
    SELECT_COLUMNS,
    VALID_AT,
    audience_filter,
)
from eidolon.memory.domain.kg import KgTripleRecord
from eidolon.memory.domain.space_lock import SpaceLock
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: How many statements one entity may contribute when the caller names no bound.
#:
#: ``query_entity`` had no LIMIT at all, so a companion's hot subject — and "用户"
#: is in most statements a companion ever records — returned the whole of it. At
#: 20 000 statements that was 13 333 rows crossing the thread boundary for a caller
#: that then sliced the first eight.
#:
#: Generous rather than tight: ``kg_max_triples_per_entity`` is 8, so this is far
#: above anything recall asks for, and it exists to bound the pathological case
#: rather than to shape results. ``ORDER_BY_RELEVANCE`` means what a truncation
#: drops is the least relevant; an operator tool that wants more passes its own.
DEFAULT_ENTITY_LIMIT = 200

#: The longest canonical name entity matching will find inside a phrase.
#:
#: Matching enumerates the query's substrings and looks each one up, so the work
#: is ``len(query) × this`` rather than the size of the graph. Sixty-four
#: characters is far above anything the steward produces — names are people,
#: places and things — and the bound is what keeps a long chat turn from
#: generating a quadratic number of lookups.
#:
#: A name longer than this is not matched by occurring in a phrase. It is still
#: reachable by every other path: an alias, a caller's ``focus_subjects``, or
#: being the subject of a statement a recalled memory produced.
MAX_MATCHABLE_NAME_LENGTH = 64

#: How many bound parameters one lookup carries.
#:
#: ``SQLITE_MAX_VARIABLE_NUMBER`` is 32 766 on the builds we run (3.46 on the
#: board, 3.53 here) but 999 on older ones, and this file has no reason to
#: require a recent SQLite for something a loop solves.
_LOOKUP_CHUNK = 900


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
    subject_id: str,
    predicate: str,
    object_id: str,
    audience: str,
    valid_from: str,
    recorded_at: str,
) -> str:
    """Derive a statement's id from its content and when it started.

    Deterministic, so the same statement recorded twice in one audience and one
    instant collides rather than duplicating. ``audience`` keeps a private fact
    distinct from a separately derived Owner fact. ``recorded_at`` is included
    because the same triple may legitimately hold over more than one interval —
    someone moves away and back — and those are different statements.
    """

    payload = "\x1f".join(
        (subject_id, predicate, object_id, audience, valid_from, recorded_at)
    )
    return f"stmt_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _substrings(text: str, max_length: int) -> list[str]:
    """Every distinct substring of ``text`` up to ``max_length``, longest first.

    The candidate keys for entity matching. Deduplicated because a phrase repeats
    substrings heavily, and longest-first so a chunked lookup tends to find the
    most specific name in the first chunk.
    """

    if not text:
        return []
    limit = min(max_length, len(text))
    pieces = {
        text[start : start + size]
        for size in range(1, limit + 1)
        for start in range(0, len(text) - size + 1)
    }
    return sorted(pieces, key=len, reverse=True)


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
        # One connection per thread, not one shared by all of them. See
        # ``_connection``.
        self._local = threading.local()
        self._open_connections: list[sqlite3.Connection] = []
        self._connections_guard = threading.Lock()
        self._initialise()

    def _connection(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use.

        Reads run in ``asyncio.to_thread`` and the space lock admits several at
        once, so "several threads reading" is the normal case. They used to share
        one ``sqlite3.Connection`` with ``check_same_thread=False`` — which permits
        cross-thread use but does not make it concurrent: SQLite serialises on the
        connection, and every row still becomes a Python object under the GIL.

        Measured at 8 000 statements, eight concurrent reads of a hot subject:

            serial                    145 ms   (18 ms each)
            concurrent, one connection  1595 ms  (199 ms each)

        **Eleven times slower than not being concurrent at all** — a mutex convoy
        plus a hundred thousand object constructions contending for the GIL. Per
        thread it was 3.3x better even before the queries were bounded.

        Chroma reached the same conclusion for its own store (``PerThreadPool``).
        The journal is already WAL, which is what makes concurrent readers
        genuinely parallel once they stop sharing a handle — the pragma was there
        and only the handle was missing.

        The executor's pool is bounded (``min(32, cores + 4)`` — eight on a Pi), so
        the number of connections is too.
        """

        connection = getattr(self._local, "conn", None)
        if connection is not None:
            return connection
        connection = sqlite3.connect(str(self._path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # Per connection, not per database, unlike journal_mode.
        connection.execute("PRAGMA foreign_keys=ON")
        self._local.conn = connection
        with self._connections_guard:
            self._open_connections.append(connection)
        return connection

    @property
    def lock(self) -> SpaceLock:
        return self._lock

    def _initialise(self) -> None:
        with self._connection() as conn:
            # WAL so a reader is never blocked by the turn currently writing.
            #
            # That was true of the file and false of the code until 2026-08-05: the
            # shared lock was an exclusive mutex, so every read here waited for the
            # turn anyway and WAL bought nothing. Reads now take the reader side,
            # which is what makes this pragma mean what it says.
            # journal_mode is a property of the database file, so it is set once
            # here and every later connection inherits it.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            for statement in SCHEMA_STATEMENTS[:3]:
                conn.execute(statement)
            mention_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(kg_entity_mentions)")
            }
            if "audience" not in mention_columns:
                conn.execute(
                    "ALTER TABLE kg_entity_mentions "
                    "ADD COLUMN audience TEXT NOT NULL DEFAULT 'owner'"
                )
            for statement in SCHEMA_STATEMENTS[3:]:
                conn.execute(statement)

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
        audience = validate_audience(audience)

        # Replaying a turn must not duplicate, and must not undo an invalidation
        # that happened after it — so this check comes before the validity one.
        if source_turn_id:
            existing = self._connection().execute(
                """
                SELECT statement_id FROM kg_statements
                WHERE space_id = ? AND source_turn_id = ?
                  AND subject_id = ? AND predicate = ? AND object_id = ?
                  AND audience = ?
                LIMIT 1
                """,
                (
                    self._space_id, source_turn_id, subject_id, predicate,
                    object_id, audience,
                ),
            ).fetchone()
            if existing is not None:
                return existing["statement_id"]

        # An identical statement that is still open is the same statement.
        still_valid = self._connection().execute(
            """
            SELECT statement_id FROM kg_statements
            WHERE space_id = ? AND subject_id = ? AND predicate = ? AND object_id = ?
              AND audience = ?
              AND valid_to IS NULL
            LIMIT 1
            """,
            (self._space_id, subject_id, predicate, object_id, audience),
        ).fetchone()
        if still_valid is not None:
            return still_valid["statement_id"]

        recorded = now_iso()
        statement_id = statement_id_for(
            subject_id, predicate, object_id, audience, started, recorded
        )
        is_sensitive = predicate_is_sensitive(predicate) if sensitive is None else sensitive

        with self._connection():
            self._upsert_entity(subject_id, subject, recorded)
            self._upsert_entity(object_id, object, recorded)
            self._connection().execute(
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
        self._connection().execute(
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
        audiences: tuple[str, ...] = (OWNER_AUDIENCE,),
        ended: str | None = None,
    ) -> int:
        async with self._lock.writer():
            return await asyncio.to_thread(
                self._invalidate_sync, subject, predicate, object, audiences, ended
            )

    def _invalidate_sync(
        self,
        subject: str,
        predicate: str,
        object: str,
        audiences: tuple[str, ...],
        ended: str | None,
    ) -> int:
        if not audiences:
            return 0
        checked = tuple(validate_audience(value) for value in audiences)
        marks = ", ".join("?" for _ in checked)
        ended_at = canonical_temporal(ended) or now_iso()
        with self._connection():
            cursor = self._connection().execute(
                f"""
                UPDATE kg_statements SET valid_to = ?
                WHERE space_id = ? AND subject_id = ? AND predicate = ? AND object_id = ?
                  AND valid_to IS NULL
                  AND audience IN ({marks})
                """,
                (
                    ended_at, self._space_id, entity_id_for(subject), predicate,
                    entity_id_for(object), *checked,
                ),
            )
        return cursor.rowcount or 0

    async def forget_source_turns(
        self,
        turn_ids: Sequence[str],
        *,
        hard: bool = False,
        ended: str | None = None,
    ) -> int:
        return await self._forget_by("source_turn_id", turn_ids, hard=hard, ended=ended)

    async def move_source_turns_to_audience(
        self, turn_ids: Sequence[str], *, audience: str
    ) -> int:
        """Move every statement from these turns to another audience.

        The graph half of "只让它记得". Without it the drawer moves and the
        triples extracted from it stay in the owner layer, so the memory would
        disappear from one Eidolon's browse and still be handed to it in the next
        prompt — the product agreeing to keep something between two people and
        then telling the third.

        Keyed on the source turn for the same reason the forget is: the turn is
        the only pointer a drawer carries to what was extracted from it.

        Unlike a forget this ends no interval and removes nothing. Statements
        stay valid, keep their history, and are still recalled — by the Eidolon
        they now belong to. So there is no ``hard`` variant and nothing to write
        into the forgotten-statements record: nothing was forgotten.
        """

        wanted = list(dict.fromkeys(v.strip() for v in turn_ids if v and v.strip()))
        if not wanted:
            return 0
        target = validate_audience(audience)
        async with self._lock.writer():
            return await asyncio.to_thread(self._move_audience_sync, wanted, target)

    def _move_audience_sync(self, turn_ids: list[str], audience: str) -> int:
        placeholders = ", ".join("?" for _ in turn_ids)
        connection = self._connection()
        with connection:
            cursor = connection.execute(
                f"""
                UPDATE kg_statements SET audience = ?
                WHERE space_id = ? AND source_turn_id IN ({placeholders})
                """,
                (audience, self._space_id, *turn_ids),
            )
        return cursor.rowcount or 0

    async def forget_statements(
        self,
        statement_ids: Sequence[str],
        *,
        hard: bool = False,
        ended: str | None = None,
    ) -> int:
        return await self._forget_by("statement_id", statement_ids, hard=hard, ended=ended)

    async def _forget_by(
        self,
        column: str,
        values: Sequence[str],
        *,
        hard: bool,
        ended: str | None,
    ) -> int:
        """Forget by turn or by statement — the same operation, two keys.

        ``column`` is chosen from a fixed pair here and never comes from a caller,
        so interpolating it into the SQL is safe. It is asserted anyway, because
        "this identifier is trusted" is the assumption that stops being true when
        someone adds a third caller.
        """

        assert column in {"source_turn_id", "statement_id"}, column
        wanted = list(dict.fromkeys(v.strip() for v in values if v and v.strip()))
        if not wanted:
            return 0
        ended_at = canonical_temporal(ended) or now_iso()
        async with self._lock.writer():
            return await asyncio.to_thread(
                self._forget_by_sync, column, wanted, hard, ended_at
            )

    def _forget_by_sync(
        self, column: str, keys: list[str], hard: bool, ended_at: str
    ) -> int:
        """Invalidate or remove every statement matching these keys.

        One statement rather than the chunked loop a background sweep would need:
        a privacy command carries at most 100 drawers and a turn yields one to
        three triples, so the whole batch is a few hundred rows and bounding the
        lock hold would cost more in round trips than it saves. A sweep over years
        of statements is a different operation and should not borrow this one.

        Both keys are indexed — ``idx_kg_statements_source`` for the turn, the
        primary key for the statement — so either is a lookup rather than a walk.
        """

        placeholders = ", ".join("?" for _ in keys)
        connection = self._connection()

        if not hard:
            # Same semantics as ``invalidate``: the row stays, its interval ends.
            # ``valid_to IS NULL`` so re-running a command counts nothing twice.
            with connection:
                cursor = connection.execute(
                    f"""
                    UPDATE kg_statements SET valid_to = ?
                    WHERE space_id = ? AND {column} IN ({placeholders})
                      AND valid_to IS NULL
                    """,
                    (ended_at, self._space_id, *keys),
                )
            return cursor.rowcount or 0

        doomed = connection.execute(
            f"""
            SELECT * FROM kg_statements
            WHERE space_id = ? AND {column} IN ({placeholders})
            """,
            (self._space_id, *keys),
        ).fetchall()
        if not doomed:
            return 0

        # Written out before anything is removed, and the write is verified by
        # reading it back. A hard forget is irreversible for the product on
        # purpose; it must not also be irreversible for whoever has to answer
        # "what did we delete on the 6th". Refusing here is the intended
        # behaviour when the record cannot be made — deleting without one is the
        # failure this is meant to prevent, not a degraded success.
        self._record_forgotten(doomed, ended_at)

        with connection:
            connection.execute(
                f"""
                DELETE FROM kg_statements
                WHERE space_id = ? AND {column} IN ({placeholders})
                """,
                (self._space_id, *keys),
            )
        # Verified rather than trusted, the way ``delete_many`` verifies on the
        # vector side. A DELETE that silently matched nothing and a DELETE that
        # worked have the same rowcount when the caller retries.
        remaining = connection.execute(
            f"""
            SELECT COUNT(*) FROM kg_statements
            WHERE space_id = ? AND {column} IN ({placeholders})
            """,
            (self._space_id, *keys),
        ).fetchone()[0]
        if remaining:
            raise RuntimeError(
                f"hard forget left {remaining} statement(s) for {len(keys)} {column}(s) "
                f"in space {self._space_id}"
            )

        # Entity rows are deliberately left. An entity may be named by statements
        # from other turns, and finding out costs a query per entity; orphan
        # collection is a sweep's job. It also means a hard forget does not remove
        # the *name* — an operator inspecting the entity table can still see that
        # someone called 张丽 was known, which is worth stating rather than
        # implying this erases every trace.
        return len(doomed)

    def _record_forgotten(self, rows: Sequence[sqlite3.Row], ended_at: str) -> None:
        """Append the rows to the forgetting log, and prove it landed.

        Beside the graph file, which is inside ``<palace>.ledgers`` and therefore
        outside the palace directory MemPalace renames during a repair. That is
        not incidental: an audit trail stored inside the thing being repaired is
        an audit trail that disappears exactly when someone needs it.

        Nothing prunes this directory and nothing should. It has to outlive what
        it describes, or the record of a deletion becomes deletable by the same
        mechanisms — which is the one property that makes "we can tell you what we
        removed" true rather than aspirational.
        """

        directory = self._path.parent / "forgotten"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{ended_at[:10]}.jsonl"
        payload = "".join(
            json.dumps(
                {"forgotten_at": ended_at, "space_id": self._space_id, **dict(row)},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
            for row in rows
        )
        with destination.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Read back rather than trust the write: an fsync that succeeded on a
        # full disk still leaves a truncated line, and the whole point of this
        # file is being readable later.
        with destination.open("r", encoding="utf-8") as handle:
            written = [line for line in handle if line.strip()]
        if len(written) < len(rows):
            raise RuntimeError(
                f"forgetting log {destination} holds {len(written)} line(s), "
                f"expected at least {len(rows)}"
            )
        json.loads(written[-1])

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
        self._invalidate_sync(subject, predicate, old_object, (audience,), boundary)
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
        audience: str = OWNER_AUDIENCE,
        confidence: float = 0.85,
    ) -> None:
        async with self._lock.writer():
            await asyncio.to_thread(
                self._record_mention_sync,
                entity_id,
                alias,
                audience,
                source,
                confidence,
            )

    def _record_mention_sync(
        self,
        entity_id: str,
        alias: str,
        audience: str,
        source: str,
        confidence: float,
    ) -> None:
        """Write an alias against the same key every other write uses.

        ``entity_id_for`` here is the fix for a silent one. This was the only
        write on the port that stored its ``entity_id`` argument raw, while
        ``add_triple`` slugs both of its entities and the read joins the two
        tables on that column:

            SELECT e.name, m.alias FROM kg_entity_mentions m
            JOIN kg_entities e ON e.entity_id = m.entity_id

        So any name that is not already its own slug produced a row that could
        never be joined to anything — written, counted in ``stats()``, and
        unreachable, with nothing logged:

            'My Dad'      -> 'my_dad'        orphaned
            'Dr. Li'      -> 'dr._li'        orphaned
            'mother:张丽' -> 'mother:张丽'   coincides
            '铁锤'        -> '铁锤'          coincides

        Which is why nobody noticed: the corpus is Chinese, and Chinese names
        have no case and no spaces, so they equal their own slugs. The feature
        worked by coincidence and would have stopped at the first Latin-script
        name — the failure being not an error but an alias that quietly resolves
        to nothing.

        Old rows are left alone. Nothing reads them today (that is the bug), a
        migration would have to guess which raw string produced which slug, and
        the only graphs that exist are empty. If a populated pre-fix graph ever
        turns up, the repair is to re-slug ``kg_entity_mentions.entity_id`` and
        drop whatever still fails to join.
        """

        canonical = entity_id_for(entity_id)
        normalised = (alias or "").strip().lower()
        if not normalised or not canonical:
            return
        checked_audience = validate_audience(audience)
        mention_id = f"{checked_audience}:{canonical}:{normalised}"
        with self._connection():
            self._connection().execute(
                """
                INSERT OR IGNORE INTO kg_entity_mentions (
                    space_id, mention_id, entity_id, alias, audience,
                    source, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._space_id, mention_id, canonical, normalised,
                    checked_audience, source, confidence, now_iso(),
                ),
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
        limit: int = DEFAULT_ENTITY_LIMIT,
    ) -> list[KgTripleRecord]:
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError(
                f"direction must be outgoing|incoming|both, got {direction!r}"
            )
        moment = canonical_temporal(as_of) or now_iso()
        async with self._lock.reader():
            return await asyncio.to_thread(
                self._query_entity_sync, name, audiences, moment, direction,
                include_sensitive, limit,
            )

    def _query_entity_sync(
        self,
        name: str,
        audiences: tuple[str, ...],
        moment: str,
        direction: str,
        include_sensitive: bool,
        limit: int,
    ) -> list[KgTripleRecord]:
        if not audiences:
            return []
        entity = entity_id_for(name)
        bound = max(1, limit)

        def branch(side: str, *, projection: str = SELECT_COLUMNS) -> tuple[str, list[Any]]:
            return (
                f"SELECT {projection} {JOIN_ENTITIES} "
                f"WHERE s.space_id = ? AND s.{side} = ? "
                f"AND {VALID_AT} "
                f"AND {audience_filter(len(audiences))} "
                f"{self._sensitive_clause(include_sensitive)} "
                f"{ORDER_BY_RELEVANCE} LIMIT ?",
                [self._space_id, entity, moment, moment, *audiences, bound],
            )

        if direction == "both":
            # Two indexed branches rather than ``subject_id = ? OR object_id = ?``.
            #
            # The OR is what a reader would write, and it is why this was the
            # slowest read in the graph. Measured at 20 000 statements, EXPLAIN
            # QUERY PLAN on each shape:
            #
            #   s.subject_id = ?   SEARCH USING INDEX idx_kg_statements_relevance_audience
            #   the OR             SEARCH USING idx_kg_statements_source (space_id=?)
            #                      + USE TEMP B-TREE FOR ORDER BY
            #
            # SQLite cannot satisfy an OR across two different indexes and one
            # ordering, so it falls back to the widest index it has and sorts
            # everything: 0.86 ms became 13.62 ms. This matters more than it
            # sounds, because ``both`` is what ``query_entity_combined`` asks for
            # and that is the default recall path.
            #
            # ``UNION`` and not ``UNION ALL``: a statement whose subject and object
            # are the same entity satisfies both branches, and the rows are
            # identical, so the set operation drops the duplicate. Each branch
            # keeps its own LIMIT, and the outer one re-orders the merged pair —
            # unprefixed, because by then the columns are the projection's aliases
            # rather than ``s.``-qualified ones.
            # The outer ORDER BY names ``recorded_at``, which it can only do
            # because the branches produce it. That used to require projecting it
            # here specially; it is part of ``SELECT_COLUMNS`` now, because the
            # renderer needs it too.
            outgoing_sql, outgoing_params = branch("subject_id")
            incoming_sql, incoming_params = branch("object_id")
            sql = (
                f"SELECT * FROM ({outgoing_sql}) "
                f"UNION "
                f"SELECT * FROM ({incoming_sql}) "
                f"ORDER BY confidence DESC, valid_from DESC, recorded_at DESC "
                f"LIMIT ?"
            )
            params = [*outgoing_params, *incoming_params, bound]
        else:
            sql, params = branch(
                "subject_id" if direction == "outgoing" else "object_id"
            )

        return [_to_record(row) for row in self._connection().execute(sql, params)]

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

        # Every subject asked about gets its own allowance. A single overall LIMIT
        # would let one well-connected entity spend the whole budget and leave the
        # others unrepresented — which is what a plain ordered query plus
        # per-subject bucketing in Python does.
        #
        # One branch per subject, each with its own ORDER BY and LIMIT, unioned
        # into one statement. This replaced a ``ROW_NUMBER() OVER (PARTITION BY
        # subject_id)`` that produced the right answer at the wrong cost: a window
        # function has to rank the *whole* partition before anything can be
        # discarded, so returning 8 rows about a companion's hot subject meant
        # sorting the 13 333 statements it had — 33 ms, measured at 20 000
        # statements, to hand back 8 records.
        #
        # A bounded branch stops instead, because ``idx_kg_statements_relevance_audience``
        # is ordered the same way the query is. Recall asks about
        # ``kg_max_entities`` subjects — three — so this is three index walks in
        # one round trip rather than one full-partition sort.
        clause = (
            f"SELECT {SELECT_COLUMNS} {JOIN_ENTITIES} "
            f"WHERE s.space_id = ? AND s.subject_id = ? "
            f"AND {VALID_AT} "
            f"AND {audience_filter(len(audiences))} "
            f"{self._sensitive_clause(include_sensitive)} "
            f"{ORDER_BY_RELEVANCE} LIMIT ?"
        )
        params: list[Any] = []
        for subject_id in wanted:
            params.extend(
                [self._space_id, subject_id, moment, moment, *audiences, limit_per_subject]
            )
        # Parenthesised so each branch keeps its own ORDER BY and LIMIT; without
        # them SQLite reads a trailing ORDER BY as applying to the whole union.
        sql = " UNION ALL ".join(f"SELECT * FROM ({clause})" for _ in wanted)
        return [_to_record(row) for row in self._connection().execute(sql, params)]

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
        current_only: bool = False,
        include_sensitive: bool = False,
    ) -> list[KgTripleRecord]:
        async with self._lock.reader():
            return await asyncio.to_thread(
                self._timeline_sync, entity_name, audiences, since, until, limit,
                current_only, include_sensitive,
            )

    def _timeline_sync(
        self,
        entity_name: str | None,
        audiences: tuple[str, ...],
        since: str | None,
        until: str | None,
        limit: int,
        current_only: bool,
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
        if current_only:
            # In the WHERE clause, not applied to the result. A caller that asks
            # for N current statements and filters a LIMIT-ed page afterwards gets
            # fewer than N as soon as the graph has any history — and cannot tell,
            # because the cap it compares against is the post-filter count.
            clauses.append("s.valid_to IS NULL")
        start = canonical_temporal(since)
        if start:
            clauses.append("(s.valid_from IS NULL OR s.valid_from >= ?)")
            params.append(start)
        end = canonical_temporal(until)
        if end:
            clauses.append("(s.valid_from IS NULL OR s.valid_from <= ?)")
            params.append(end)
        clauses.append(audience_filter(len(audiences)))
        params.extend(audiences)
        sql = (
            f"SELECT {SELECT_COLUMNS} {JOIN_ENTITIES} "
            f"WHERE {' AND '.join(clauses)} "
            f"{self._sensitive_clause(include_sensitive)} "
            "ORDER BY s.valid_from DESC, s.recorded_at DESC LIMIT ?"
        )
        params.append(max(1, limit))
        return [_to_record(row) for row in self._connection().execute(sql, params)]

    async def entities_for_source_turns(
        self, turn_ids: Sequence[str], *, cap: int
    ) -> list[str]:
        wanted = list(dict.fromkeys(t.strip() for t in turn_ids if t and t.strip()))
        if not wanted or cap <= 0:
            return []
        async with self._lock.reader():
            return await asyncio.to_thread(self._entities_for_turns_sync, wanted, cap)

    def _entities_for_turns_sync(self, turn_ids: list[str], cap: int) -> list[str]:
        """The entities named by these turns' statements, busiest first.

        Both ends of every statement, because a turn is about its objects as much
        as its subjects — "妈妈住在杭州" is a turn about 妈妈 *and* about 杭州, and
        which one the next question is about is not ours to guess.

        Ordered by how many of these turns mention each entity. When several turns
        come back from one recall they usually share a subject, and that shared one
        is what the conversation is about; an arbitrary slice of a ``DISTINCT``
        would drop it as readily as anything else.

        ``idx_kg_statements_source`` covers the predicate, so this is a seek per
        turn rather than a walk.
        """

        connection = self._connection()
        counted: dict[str, int] = {}
        for start in range(0, len(turn_ids), _LOOKUP_CHUNK):
            chunk = turn_ids[start : start + _LOOKUP_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT e.name AS name, COUNT(*) AS hits
                FROM kg_statements s
                JOIN kg_entities e
                  ON e.space_id = s.space_id
                 AND e.entity_id IN (s.subject_id, s.object_id)
                WHERE s.space_id = ? AND s.source_turn_id IN ({marks})
                GROUP BY e.name
                """,
                (self._space_id, *chunk),
            )
            for row in rows:
                name = row["name"]
                if name:
                    counted[name] = counted.get(name, 0) + int(row["hits"])
        ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0]))
        return [name for name, _hits in ranked[:cap]]

    async def match_entities_for_query(
        self,
        query: str,
        *,
        cap: int,
        audiences: tuple[str, ...] = (OWNER_AUDIENCE,),
    ) -> list[str]:
        if cap <= 0 or not audiences:
            return []
        async with self._lock.reader():
            return await asyncio.to_thread(
                self._match_entities_sync, query, audiences, cap
            )

    def _match_entities_sync(
        self, query: str, audiences: tuple[str, ...], cap: int
    ) -> list[str]:
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

        **The question is turned around so an index can answer it.**

        "Does this stored name occur in the phrase" puts the wildcard on the
        stored side, and no B-tree serves that — which is why this was a scan for
        as long as it existed, and why the previous note here concluded a trigram
        FTS table was the only way out. It is not. The test is equivalent to one
        an index answers directly:

            name occurs in phrase  ⟺  name equals some substring of phrase

        So the substrings are enumerated and looked up, instead of the names being
        enumerated and tested. The work becomes ``len(phrase) × the longest name
        worth matching`` and stops depending on the size of the graph — which was
        the whole problem, since matching scans ``kg_entities`` and nothing has
        ever deleted an entity row.

        Measured against 80 000 entities: 0.09 ms for the canonical lookup and
        0.12 ms for the tail, where the scan cost 25.69 ms on the board at 70 000
        and grew from there. Both are covering-index seeks.

        **Exactly equivalent, not approximately.** No threshold, no similarity, no
        candidate set to re-verify: the same names come back in the same order,
        which is what makes this safe to do to a hard gate whose results are
        rendered into the model's prompt as assertions. Case sensitivity survives
        too — SQLite compares TEXT with BINARY collation by default, matching the
        ``instr()`` this replaces.
        """

        text = (query or "").strip()
        if not text or cap <= 0:
            return []

        pieces = _substrings(text, MAX_MATCHABLE_NAME_LENGTH)
        if not pieces:
            return []

        found: list[str] = []
        seen: set[str] = set()

        # Whole name first, then the tail after a type prefix — the steward writes
        # ``pet:铁锤`` to keep a dog distinct from a person, and someone asking
        # about the dog just says 铁锤. ``instr(name, ':') > 0`` in the tail
        # lookup keeps a name that is nothing but a prefix from matching, the same
        # rule ``name_appears_in`` states, so both sides of the port agree.
        #
        # Longest first within each strategy, so ``mother:张丽`` beats a bare
        # ``mother`` when both could fire.
        for sql in (
            "SELECT DISTINCT name FROM kg_entities "
            "WHERE space_id = ? AND name <> '' AND name IN ({marks}) "
            "ORDER BY length(name) DESC LIMIT ?",
            "SELECT DISTINCT name FROM kg_entities "
            "WHERE space_id = ? AND instr(name, ':') > 0 "
            "  AND substr(name, instr(name, ':') + 1) IN ({marks}) "
            "ORDER BY length(name) DESC LIMIT ?",
        ):
            for name in self._lookup_names(sql, pieces, cap):
                if name and name not in seen:
                    seen.add(name)
                    found.append(name)
            if len(found) >= cap:
                return found[:cap]

        # Aliases are stored lowercased, so the phrase is lowered to match — the
        # one place this comparison is deliberately case-insensitive.
        lowered = _substrings(text.lower(), MAX_MATCHABLE_NAME_LENGTH)
        alias_sql = (
            "SELECT e.name AS name, m.alias AS alias FROM kg_entity_mentions m "
            "JOIN kg_entities e ON e.space_id = m.space_id AND e.entity_id = m.entity_id "
            "WHERE m.space_id = ? AND m.alias <> '' AND m.alias IN ({marks}) "
            f"AND m.audience IN ({', '.join('?' for _ in audiences)}) "
            "ORDER BY length(m.alias) DESC LIMIT ?"
        )
        for name in self._lookup_names(
            alias_sql, lowered, cap, trailing_params=list(audiences)
        ):
            if not name or name in seen:
                continue
            seen.add(name)
            found.append(name)
            if len(found) >= cap:
                break
        return found[:cap]

    def _lookup_names(
        self,
        sql: str,
        pieces: list[str],
        cap: int,
        trailing_params: list[str] | None = None,
    ) -> list[str]:
        """Run one lookup over ``pieces``, chunked, longest name first.

        Chunking splits the ordering, so the chunks are re-sorted here rather
        than trusted: ``ORDER BY`` inside a chunk only orders that chunk, and the
        caller's "longest wins" rule is about the whole result.
        """

        connection = self._connection()
        names: list[str] = []
        for start in range(0, len(pieces), _LOOKUP_CHUNK):
            chunk = pieces[start : start + _LOOKUP_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            rows = connection.execute(
                sql.format(marks=marks),
                (self._space_id, *chunk, *(trailing_params or []), cap),
            )
            names.extend(row["name"] for row in rows)
        names.sort(key=len, reverse=True)
        return names

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
            for row in self._connection().execute(
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
            for row in self._connection().execute(
                "SELECT name FROM kg_entities WHERE space_id = ? ORDER BY name",
                (self._space_id,),
            )
        ]

    async def stats(self) -> dict[str, Any]:
        async with self._lock.reader():
            return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, Any]:
        row = self._connection().execute(
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
            self._connection().execute(
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
        row = self._connection().execute(
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
        row = self._connection().execute(
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
        """Close every connection this graph opened, not just this thread's.

        Each reader thread has its own, so closing the caller's would leave the
        rest holding the file — which on a space being torn down means the palace
        directory cannot be moved and the next process's claim looks contended.
        """

        with self._connections_guard:
            connections, self._open_connections = self._open_connections, []
        self._local = threading.local()
        for connection in connections:
            try:
                connection.close()
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
        recorded_at=row[9],
    )


__all__ = [
    "SqliteKnowledgeGraph",
    "canonical_temporal",
    "entity_id_for",
    "now_iso",
    "predicate_is_sensitive",
    "statement_id_for",
]
