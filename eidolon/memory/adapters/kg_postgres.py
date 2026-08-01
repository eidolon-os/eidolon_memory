"""The knowledge graph in a shared database, for replicas that own nothing.

This is the half of the cloud shape that a file cannot provide. Vectors already
live on a server; the graph was the remaining thing pinned to one host's disk, and
a replica keeping it locally would stop being interchangeable with its peers.

Almost nothing here is new logic. The schema and the query shapes come from
:mod:`eidolon.memory.adapters.kg_sql`, the same ones the file-backed graph uses,
because the workload is single-hop lookups over intervals and both stores do that
identically. What differs is narrow and named: the parameter marker, the upsert
clause, and a connection pool in place of one connection behind a lock.

No lock. The database serialises for us, and holding an asyncio lock across a
network round trip would turn concurrent recalls for different spaces into a
queue — which is exactly the coupling the shared-store shape exists to remove.

Verification status, stated plainly: the SQL these two implementations share is
exercised against SQLite, and a test asserts the two generate structurally
matching statements with the same parameter counts. What is *not* verified here is
this file against a live PostgreSQL — that needs a server, and
``tests/memory/test_live_postgres_kg.py`` runs it when one is configured.
"""

from __future__ import annotations

from typing import Any

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
from eidolon.memory.adapters.kg_sqlite import (
    canonical_temporal,
    entity_id_for,
    now_iso,
    predicate_is_sensitive,
    statement_id_for,
)
from eidolon.memory.domain.kg import KgTripleRecord
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

#: PostgreSQL's placeholder. SQLite uses ``?``; this is the whole of the
#: parameter-marker difference, which is why the shared SQL takes it as an
#: argument rather than hardcoding either.
MARKER = "%s"

#: SQLite spells a conflict-tolerant insert ``INSERT OR IGNORE``; PostgreSQL
#: spells it with a conflict target. The target must name the primary key, or a
#: genuine duplicate would raise instead of being ignored.
ON_CONFLICT_IGNORE = "ON CONFLICT (space_id, statement_id) DO NOTHING"
ON_CONFLICT_IGNORE_ENTITY = "ON CONFLICT (space_id, entity_id) DO NOTHING"
ON_CONFLICT_IGNORE_MENTION = "ON CONFLICT (space_id, mention_id) DO NOTHING"


def postgres_schema() -> tuple[str, ...]:
    """The shared schema, in PostgreSQL's dialect.

    Only two substitutions are needed. ``INTEGER`` for the sensitivity flag
    becomes ``BOOLEAN``, because Postgres has one and comparing an integer to a
    boolean is an error rather than a coercion. And SQLite's ``IF NOT EXISTS`` on
    an index is spelled the same, so nothing changes there.
    """

    return tuple(
        statement.replace(
            "sensitive      INTEGER NOT NULL DEFAULT 0",
            "sensitive      BOOLEAN NOT NULL DEFAULT FALSE",
        )
        for statement in SCHEMA_STATEMENTS
    )


class PostgresKnowledgeGraph:
    """A space's graph in a shared PostgreSQL database.

    ``space_id`` scopes every statement. Unlike the file-backed graph, where the
    column is defence in depth behind a per-space file, here it is the only thing
    separating one owner's graph from another's — so it is on every query rather
    than assumed.
    """

    # The store handles concurrency; see the module docstring on why holding a
    # lock across a round trip would be actively harmful.
    lock = None

    def __init__(self, pool: Any, *, space_id: str) -> None:
        self._pool = pool
        self._space_id = space_id

    @classmethod
    async def connect(cls, dsn: str, *, space_id: str, min_size: int = 1, max_size: int = 8):
        """Open a pool and ensure the schema exists.

        Pooled rather than one connection per graph: a replica serves every space,
        so per-space connections would multiply with tenants rather than with
        concurrency.
        """

        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "kg.backend='postgres' needs the 'postgres' extra "
                "(pip install 'eidolon-memory[postgres]')"
            ) from exc

        pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        graph = cls(pool, space_id=space_id)
        await graph.ensure_schema()
        return graph

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            for statement in postgres_schema():
                await conn.execute(statement)

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
        subject_id = entity_id_for(subject)
        object_id = entity_id_for(object)
        started = canonical_temporal(valid_from) or now_iso()
        ended = canonical_temporal(valid_to)

        async with self._pool.connection() as conn:
            # One transaction so the two idempotency checks and the insert cannot
            # interleave with a concurrent writer for the same statement.
            async with conn.transaction():
                if source_turn_id:
                    found = await self._scalar(
                        conn,
                        f"""
                        SELECT statement_id FROM kg_statements
                        WHERE space_id = {MARKER} AND source_turn_id = {MARKER}
                          AND subject_id = {MARKER} AND predicate = {MARKER}
                          AND object_id = {MARKER}
                        LIMIT 1
                        """,
                        (self._space_id, source_turn_id, subject_id, predicate, object_id),
                    )
                    if found:
                        return found

                still_valid = await self._scalar(
                    conn,
                    f"""
                    SELECT statement_id FROM kg_statements
                    WHERE space_id = {MARKER} AND subject_id = {MARKER}
                      AND predicate = {MARKER} AND object_id = {MARKER}
                      AND valid_to IS NULL
                    LIMIT 1
                    """,
                    (self._space_id, subject_id, predicate, object_id),
                )
                if still_valid:
                    return still_valid

                recorded = now_iso()
                statement_id = statement_id_for(
                    subject_id, predicate, object_id, started, recorded
                )
                is_sensitive = (
                    predicate_is_sensitive(predicate) if sensitive is None else sensitive
                )

                for entity_id, name in ((subject_id, subject), (object_id, object)):
                    await conn.execute(
                        f"""
                        INSERT INTO kg_entities (
                            space_id, entity_id, name, entity_type, properties, created_at
                        ) VALUES ({MARKER}, {MARKER}, {MARKER}, 'unknown', '{{}}', {MARKER})
                        {ON_CONFLICT_IGNORE_ENTITY}
                        """,
                        (self._space_id, entity_id, (name or "").strip(), recorded),
                    )

                await conn.execute(
                    f"""
                    INSERT INTO kg_statements (
                        space_id, statement_id, subject_id, predicate, object_id,
                        audience, sensitive, valid_from, valid_to, recorded_at,
                        confidence, source_turn_id, adapter_name
                    ) VALUES ({", ".join([MARKER] * 13)})
                    {ON_CONFLICT_IGNORE}
                    """,
                    (
                        self._space_id, statement_id, subject_id, predicate, object_id,
                        audience, is_sensitive, started, ended, recorded,
                        confidence, source_turn_id, adapter_name,
                    ),
                )
                return statement_id

    async def invalidate(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        ended: str | None = None,
    ) -> int:
        ended_at = canonical_temporal(ended) or now_iso()
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                UPDATE kg_statements SET valid_to = {MARKER}
                WHERE space_id = {MARKER} AND subject_id = {MARKER}
                  AND predicate = {MARKER} AND object_id = {MARKER}
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
        """End one statement and begin another at the same instant.

        Unlike the file-backed graph, which relies on a held lock, this depends on
        the transaction: without it a reader between the two writes would see the
        fact as absent rather than changed.
        """

        boundary = canonical_temporal(changed_at) or now_iso()
        await self.invalidate(
            subject=subject, predicate=predicate, object=old_object, ended=boundary
        )
        return await self.add_triple(
            subject=subject,
            predicate=predicate,
            object=new_object,
            audience=audience,
            valid_from=boundary,
            confidence=confidence,
            source_turn_id=source_turn_id,
        )

    async def record_entity_mention(
        self,
        *,
        entity_id: str,
        alias: str,
        source: str,
        confidence: float = 0.85,
    ) -> None:
        normalised = (alias or "").strip().lower()
        if not normalised or not entity_id:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""
                INSERT INTO kg_entity_mentions (
                    space_id, mention_id, entity_id, alias, source, confidence, created_at
                ) VALUES ({", ".join([MARKER] * 7)})
                {ON_CONFLICT_IGNORE_MENTION}
                """,
                (self._space_id, f"{entity_id}:{normalised}", entity_id, normalised,
                 source, confidence, now_iso()),
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
        if not audiences:
            return []
        moment = canonical_temporal(as_of) or now_iso()
        entity = entity_id_for(name)

        side = {
            "outgoing": f"s.subject_id = {MARKER}",
            "incoming": f"s.object_id = {MARKER}",
            "both": f"(s.subject_id = {MARKER} OR s.object_id = {MARKER})",
        }[direction]
        params: list[Any] = [self._space_id]
        params.extend([entity, entity] if direction == "both" else [entity])
        params.extend([moment, moment, *audiences])

        sql = (
            f"SELECT {SELECT_COLUMNS} {JOIN_ENTITIES} "
            f"WHERE s.space_id = {MARKER} AND {side} "
            f"AND {VALID_AT.format(p=MARKER)} "
            f"AND {audience_filter(len(audiences), MARKER)} "
            f"{_sensitive_clause(include_sensitive)} "
            f"{ORDER_BY_RELEVANCE}"
        )
        return await self._records(sql, params)

    async def query_subjects(
        self,
        names: list[str],
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        include_sensitive: bool = False,
        limit_per_subject: int = 8,
    ) -> list[KgTripleRecord]:
        if not names or not audiences or limit_per_subject <= 0:
            return []
        wanted = [entity_id_for(name) for name in names if name and name.strip()]
        if not wanted:
            return []

        moment = canonical_temporal(as_of) or now_iso()
        placeholders = ", ".join(MARKER for _ in wanted)
        sql = (
            f"SELECT {RANKED_SUBJECT_COLUMNS} FROM ("
            f"  SELECT {SELECT_COLUMNS}, {SUBJECT_RANK} {JOIN_ENTITIES} "
            f"  WHERE s.space_id = {MARKER} AND s.subject_id IN ({placeholders}) "
            f"  AND {VALID_AT.format(p=MARKER)} "
            f"  AND {audience_filter(len(audiences), MARKER)} "
            f"  {_sensitive_clause(include_sensitive)}"
            f") ranked WHERE ranked.subject_rank <= {MARKER}"
        )
        params: list[Any] = [
            self._space_id, *wanted, moment, moment, *audiences, limit_per_subject
        ]
        return await self._records(sql, params)

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
        if not audiences:
            return []
        clauses = [f"s.space_id = {MARKER}"]
        params: list[Any] = [self._space_id]
        if entity_name:
            entity = entity_id_for(entity_name)
            clauses.append(f"(s.subject_id = {MARKER} OR s.object_id = {MARKER})")
            params.extend([entity, entity])
        start = canonical_temporal(since)
        if start:
            clauses.append(f"(s.valid_from IS NULL OR s.valid_from >= {MARKER})")
            params.append(start)
        end = canonical_temporal(until)
        if end:
            clauses.append(f"(s.valid_from IS NULL OR s.valid_from <= {MARKER})")
            params.append(end)
        clauses.append(audience_filter(len(audiences), MARKER))
        params.extend(audiences)

        sql = (
            f"SELECT {SELECT_COLUMNS} {JOIN_ENTITIES} "
            f"WHERE {' AND '.join(clauses)} "
            f"{_sensitive_clause(include_sensitive)} "
            f"ORDER BY s.valid_from DESC, s.recorded_at DESC LIMIT {MARKER}"
        )
        params.append(max(1, limit))
        return await self._records(sql, params)

    async def match_entities_for_query(self, query: str, *, cap: int) -> list[str]:
        """Same two strategies as the file-backed graph, one round trip each.

        Names and aliases are fetched rather than matched in SQL because the
        matching is substring containment in either direction — a canonical name
        appearing in the query, not the reverse — which an index cannot serve.
        Bounded by how many entities a space has, which is small.
        """

        text = (query or "").strip()
        if not text or cap <= 0:
            return []

        found: list[str] = []
        seen: set[str] = set()

        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT name FROM kg_entities WHERE space_id = {MARKER}",
                (self._space_id,),
            )
            names = [row[0] for row in await cursor.fetchall()]

            for name in sorted(set(names), key=lambda value: -len(value)):
                if name not in seen and name_appears_in(name, text):
                    found.append(name)
                    seen.add(name)
                    if len(found) >= cap:
                        return found

            cursor = await conn.execute(
                f"""
                SELECT m.alias, e.name FROM kg_entity_mentions m
                JOIN kg_entities e
                  ON e.space_id = m.space_id AND e.entity_id = m.entity_id
                WHERE m.space_id = {MARKER}
                """,
                (self._space_id,),
            )
            aliases = list(await cursor.fetchall())

        lowered = text.lower()
        for alias, name in sorted(aliases, key=lambda pair: -len(pair[0] or "")):
            if alias and name not in seen and alias in lowered:
                found.append(name)
                seen.add(name)
                if len(found) >= cap:
                    break
        return found

    async def known_audiences(self) -> list[str]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT DISTINCT audience FROM kg_statements WHERE space_id = {MARKER}",
                (self._space_id,),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def list_entity_names(self) -> list[str]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT name FROM kg_entities WHERE space_id = {MARKER} ORDER BY name",
                (self._space_id,),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def stats(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT
                    (SELECT COUNT(*) FROM kg_entities WHERE space_id = {MARKER}),
                    (SELECT COUNT(*) FROM kg_statements WHERE space_id = {MARKER}),
                    (SELECT COUNT(*) FROM kg_statements
                       WHERE space_id = {MARKER} AND valid_to IS NULL),
                    (SELECT COUNT(*) FROM kg_entity_mentions WHERE space_id = {MARKER})
                """,
                (self._space_id,) * 4,
            )
            entities, total, active, mentions = await cursor.fetchone()
        return {
            "entities": entities,
            "triples_total": total,
            "triples_active": active,
            "triples_invalidated": total - active,
            "mentions": mentions,
        }

    # ── idempotency probes ──────────────────────────────────────────────────

    async def has_triple(self, triple_id: str) -> bool:
        async with self._pool.connection() as conn:
            found = await self._scalar(
                conn,
                f"SELECT statement_id FROM kg_statements "
                f"WHERE space_id = {MARKER} AND statement_id = {MARKER}",
                (self._space_id, triple_id),
            )
        return found is not None

    async def find_pending_triple_id(
        self, source_turn_id: str, subject: str, predicate: str, object: str
    ) -> str | None:
        async with self._pool.connection() as conn:
            return await self._scalar(
                conn,
                f"""
                SELECT statement_id FROM kg_statements
                WHERE space_id = {MARKER} AND source_turn_id = {MARKER}
                  AND subject_id = {MARKER} AND predicate = {MARKER}
                  AND object_id = {MARKER}
                LIMIT 1
                """,
                (self._space_id, source_turn_id, entity_id_for(subject), predicate,
                 entity_id_for(object)),
            )

    async def find_invalidation_applied(
        self, subject: str, predicate: str, object: str, ended_at_or_before: str
    ) -> bool:
        boundary = canonical_temporal(ended_at_or_before) or now_iso()
        async with self._pool.connection() as conn:
            found = await self._scalar(
                conn,
                f"""
                SELECT statement_id FROM kg_statements
                WHERE space_id = {MARKER} AND subject_id = {MARKER}
                  AND predicate = {MARKER} AND object_id = {MARKER}
                  AND valid_to IS NOT NULL AND valid_to <= {MARKER}
                LIMIT 1
                """,
                (self._space_id, entity_id_for(subject), predicate,
                 entity_id_for(object), boundary),
            )
        return found is not None

    # ── plumbing ────────────────────────────────────────────────────────────

    async def _records(self, sql: str, params: list[Any]) -> list[KgTripleRecord]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
        return [
            KgTripleRecord(
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
            for row in rows
        ]

    @staticmethod
    async def _scalar(conn: Any, sql: str, params: tuple) -> Any:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return row[0] if row else None

    def close(self) -> None:
        """Pools close asynchronously; this is a no-op for interface parity.

        Kept so a caller can treat both graphs the same. Use ``aclose`` where the
        pool's own shutdown matters.
        """

    async def aclose(self) -> None:
        await self._pool.close()


def _sensitive_clause(include_sensitive: bool) -> str:
    """Filter sensitivity in the query, as the file-backed graph does.

    ``FALSE`` rather than ``0``: the column is boolean here, and Postgres will not
    compare the two.
    """

    return "" if include_sensitive else "AND s.sensitive = FALSE"
