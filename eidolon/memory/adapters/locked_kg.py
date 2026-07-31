"""asyncio.Lock-wrapped mempalace KnowledgeGraph (D1 + plan §3.0).

Adds three things on top of the bundled ``mempalace.KnowledgeGraph``:

1. **Shared lock** with ``LockedBackend`` so chroma and KG reads/writes stay
   coherent inside one agent_runner process.
2. **Source-id idempotency** for ``add_triple`` — mempalace's own ``add_triple``
   already short-circuits when a still-valid identical triple exists, but it
   misses the "added → invalidated → original turn replayed" edge case
   (NAK retry, JetStream rebuild). Our wrapper also checks the
   ``source_drawer_id`` (used as ``source_turn_id``) so the same turn can
   never produce two triples.
3. **Sensitive-predicate filter** for the public read tools so health-related
   predicates only surface with explicit opt-in (plan §3.3 G2).

Schema reference (mempalace/knowledge_graph.py, paraphrased)::

    entities(id PK, name, type, properties, created_at)
    triples(id PK, subject FK, predicate, object FK,
            valid_from, valid_to, confidence,
            source_closet, source_file, source_drawer_id, adapter_name,
            extracted_at)
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from eidolon_memory_contracts import SENSITIVE_PREDICATES

from eidolon.memory.domain.kg import (
    KgEntityRecord,
    KgTripleRecord,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def _now_iso() -> str:
    """ISO-8601 timestamp accepted by ``mempalace.config.sanitize_iso_temporal``.

    mempalace requires ``YYYY-MM-DDTHH:MM:SSZ`` (no microseconds, ``Z`` suffix
    rather than ``+00:00``). Be strict so every callsite produces the same
    canonical form — KG queries compare timestamps as strings.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _canonical_temporal(value: str | None) -> str | None:
    """Return a KG-safe temporal string or preserve unknown inputs.

    MemPalace accepts date-only values and UTC datetimes without microseconds
    using a trailing ``Z``. Agent and SDK payloads may carry Python ISO strings
    such as ``2026-06-28T11:41:17.964620+00:00``; normalize those at the KG
    boundary so every upstream writer gets the same contract.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _DATE_ONLY_RE.match(text):
        return text
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class LockedKnowledgeGraph:
    """Single-process lock-guarded wrapper over ``mempalace.KnowledgeGraph``."""

    def __init__(self, inner: Any, lock: asyncio.Lock) -> None:
        self._inner = inner  # mempalace.knowledge_graph.KnowledgeGraph
        self._lock = lock
        # Phase 3: ensure the entity_mentions table exists. Idempotent — safe
        # on every spawn, including replay of a long-lived palace. Runs once
        # at construction so the rest of the class can assume the schema is
        # in place without per-call existence checks.
        self._ensure_entity_mentions_schema()

    def _ensure_entity_mentions_schema(self) -> None:
        """Create the alias index table + supporting indexes if absent.

        Why a separate table vs. a JSON column on ``entities``: aliases are
        looked up by reverse query (``WHERE alias IN (...)``) on the recall
        hot path. A dedicated indexed table makes that O(log n); JSON ops on
        SQLite are O(n).
        """
        conn = self._inner._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entity_mentions (
                id          TEXT PRIMARY KEY,
                entity_id   TEXT NOT NULL,
                alias       TEXT NOT NULL,
                source      TEXT NOT NULL,
                confidence  REAL DEFAULT 0.85,
                created_at  TEXT NOT NULL,
                UNIQUE(entity_id, alias)
            );
            CREATE INDEX IF NOT EXISTS idx_mentions_alias  ON entity_mentions(alias);
            CREATE INDEX IF NOT EXISTS idx_mentions_entity ON entity_mentions(entity_id);
            """
        )
        conn.commit()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def inner(self) -> Any:
        return self._inner

    # ── Write path ──────────────────────────────────────────────────────────

    async def add_triple(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        confidence: float = 1.0,
        source_turn_id: str | None = None,
        adapter_name: str | None = None,
    ) -> str:
        """Idempotent add. Returns the triple id (existing or newly created).

        Idempotency contract:
          - Same ``source_turn_id`` + ``(subject, predicate, object)`` → no-op,
            returns the previously-recorded id.
          - mempalace itself dedups by ``valid_to IS NULL`` equality on
            ``(subject, predicate, object)``.
          - Combined: replay-after-invalidation does **not** re-add the same
            fact, because the source_turn_id check fires first.
        """
        vf = _canonical_temporal(valid_from) or _now_iso()
        vt = _canonical_temporal(valid_to)

        async with self._lock:
            # 1) source-id dedup (handles invalidate-then-replay edge case)
            if source_turn_id:
                existing = await asyncio.to_thread(
                    self._find_by_source_and_triple,
                    subject, predicate, object, source_turn_id,
                )
                if existing:
                    return existing

            # 2) delegate to mempalace (which handles "still-valid duplicate")
            return await asyncio.to_thread(
                self._inner.add_triple,
                subject, predicate, object,
                vf, vt, confidence,
                None,         # source_closet
                None,         # source_file
                source_turn_id,
                adapter_name,
            )

    async def invalidate(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        ended: str | None = None,
    ) -> int:
        """End a triple's validity. Idempotent (no-op when already ended).

        Returns number of rows updated (0 = no matching active triple).
        """
        ended_iso = _canonical_temporal(ended) or _now_iso()
        async with self._lock:
            return await asyncio.to_thread(
                self._invalidate_count, subject, predicate, object, ended_iso
            )

    # ── Read path ───────────────────────────────────────────────────────────

    async def query_entity(
        self,
        name: str,
        *,
        as_of: str | None = None,
        direction: str = "outgoing",
        include_sensitive: bool = False,
    ) -> list[KgTripleRecord]:
        """Return triples connected to entity ``name`` at point ``as_of``."""
        if direction not in {"outgoing", "incoming", "both"}:
            msg = f"direction must be outgoing|incoming|both, got {direction!r}"
            raise ValueError(msg)
        as_of_iso = _canonical_temporal(as_of) or _now_iso()
        async with self._lock:
            rows = await asyncio.to_thread(
                self._query_entity_rows, name, as_of_iso, direction
            )
        return _filter_sensitive(rows, include_sensitive)

    async def query_entity_combined(
        self,
        names: list[str],
        *,
        as_of: str | None = None,
        include_sensitive: bool = False,
        limit_per_entity: int = 8,
    ) -> list[KgTripleRecord]:
        """T3 recall hot path: one SQL with ``subject IN (?,?,?)``.

        Faster than calling :meth:`query_entity` N times because it's a single
        SQLite round-trip + single index seek.
        """
        if not names:
            return []
        as_of_iso = _canonical_temporal(as_of) or _now_iso()
        async with self._lock:
            rows = await asyncio.to_thread(
                self._query_combined_rows, names, as_of_iso, limit_per_entity
            )
        return _filter_sensitive(rows, include_sensitive)

    async def query_subjects(
        self,
        names: list[str],
        *,
        as_of: str | None = None,
        include_sensitive: bool = False,
        limit_per_subject: int = 8,
    ) -> list[KgTripleRecord]:
        """Return bounded current outgoing facts for explicit subjects.

        Unlike entity discovery, this read does not inspect query language and
        does not include triples where the requested entity is only an object.
        """
        if not names or limit_per_subject <= 0:
            return []
        names = list(dict.fromkeys(names))
        as_of_iso = _canonical_temporal(as_of) or _now_iso()
        async with self._lock:
            rows = await asyncio.to_thread(
                self._query_subject_rows, names, as_of_iso, limit_per_subject
            )
        return _filter_sensitive(rows, include_sensitive)

    async def timeline(
        self,
        entity_name: str | None = None,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        include_sensitive: bool = False,
    ) -> list[KgTripleRecord]:
        """Chronological events; entity-scoped if name given, else global."""
        async with self._lock:
            rows = await asyncio.to_thread(
                self._timeline_rows,
                entity_name,
                _canonical_temporal(since),
                _canonical_temporal(until),
                limit,
            )
        return _filter_sensitive(rows, include_sensitive)

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._stats)

    async def list_entity_names(self) -> list[str]:
        """Return every canonical entity name. Prefer
        :meth:`match_entities_for_query` for recall routing — this raw list
        is only useful for diagnostics / admin tooling.
        """
        async with self._lock:
            return await asyncio.to_thread(self._entity_names)

    async def record_entity_mention(
        self,
        *,
        entity_id: str,
        alias: str,
        source: str,
        confidence: float = 0.85,
    ) -> None:
        """Idempotent write to ``entity_mentions``.

        Idempotency via ``UNIQUE(entity_id, alias)`` — replays / re-deliveries
        from JetStream collapse to the original row without raising. The
        deterministic id is ``<entity_id>::<sha8(alias)>`` so the same alias
        always maps to the same row.
        """
        if not entity_id or not alias:
            return
        async with self._lock:
            await asyncio.to_thread(
                self._insert_entity_mention, entity_id, alias, source, confidence
            )

    def _insert_entity_mention(
        self, entity_id: str, alias: str, source: str, confidence: float
    ) -> None:
        mention_id = f"{entity_id}::{hashlib.sha256(alias.encode()).hexdigest()[:8]}"
        conn = self._inner._conn()
        conn.execute(
            "INSERT OR IGNORE INTO entity_mentions"
            "(id, entity_id, alias, source, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mention_id, entity_id, alias, source, float(confidence), _now_iso()),
        )
        conn.commit()

    def _alias_rows(self) -> list[tuple[str, str]]:
        """Return ``[(alias, entity_id), ...]`` — typical palace has <200 rows."""
        rows = self._inner._conn().execute(
            "SELECT alias, entity_id FROM entity_mentions"
        ).fetchall()
        # Tolerate both sqlite Row and bare tuples.
        return [(r["alias"], r["entity_id"]) if hasattr(r, "keys") else (r[0], r[1])
                for r in rows]

    async def match_entities_for_query(
        self, query: str, *, cap: int
    ) -> list[str]:
        """Return canonical entity names that "appear" in ``query``.

        Owns the naming-convention bridge: steward writes entities with type
        prefixes (``pet:铁锤``, ``place:北京``, ``mother:张丽``) for
        disambiguation, but users ask naturally with bare names ("铁锤"
        "北京" "妈妈"). The recall router is upstream of this layer and
        shouldn't have to know about ``pet:`` vs ``铁锤`` — the KG facade
        bridges it.

        Matching strategies (in priority order — canonical hits win):
          1. literal containment of canonical name  — ``name in query``
          2. prefix-stripped tail of canonical name — ``tail(name) in query``
          3. (Phase 3) alias reverse lookup        — ``mention.alias in query``

        Within each strategy, longer matches are preferred so ``mother:张丽``
        wins over the bare ``mother`` when both could fire. Cross-strategy
        de-duplication keeps canonical hits and never re-emits the same
        entity via its alias.

        ``cap`` truncates after sorting so pathological queries that mention
        every entity in the palace don't blow up downstream SQL.
        """
        q = (query or "").strip()
        if not q or cap <= 0:
            return []
        async with self._lock:
            names = await asyncio.to_thread(self._entity_names)
            # Same critical section — alias rows live in the same SQLite file
            # as entities; pull both under one lock acquisition.
            alias_rows = await asyncio.to_thread(self._alias_rows)

        hits: list[str] = []
        seen: set[str] = set()

        # Strategies 1+2: canonical names (prefixed or bare).
        for name in sorted(set(names), key=lambda n: -len(n)):
            if name in seen:
                continue
            if _entity_name_in_query(name, q):
                hits.append(name)
                seen.add(name)
                if len(hits) >= cap:
                    return hits

        # Strategy 3: alias reverse lookup. Longest alias first so "我老婆" beats
        # the substring "老婆", and entities already matched canonically aren't
        # re-emitted via their aliases.
        for alias, entity_id in sorted(alias_rows, key=lambda r: -len(r[0])):
            if entity_id in seen or not alias:
                continue
            if alias in q:
                hits.append(entity_id)
                seen.add(entity_id)
                if len(hits) >= cap:
                    break

        return hits

    async def has_triple(self, triple_id: str) -> bool:
        """Used by MCP admin tools to poll for write visibility."""
        async with self._lock:
            row = await asyncio.to_thread(
                lambda: self._inner._conn().execute(
                    "SELECT 1 FROM triples WHERE id = ?", (triple_id,)
                ).fetchone()
            )
        return row is not None

    async def find_pending_triple_id(
        self, source_turn_id: str, subject: str, predicate: str, object: str
    ) -> str | None:
        """Return the triple id created for this command (for MCP polling)."""
        async with self._lock:
            return await asyncio.to_thread(
                self._find_by_source_and_triple,
                subject, predicate, object, source_turn_id,
            )

    async def find_invalidation_applied(
        self,
        subject: str,
        predicate: str,
        object: str,
        ended_at_or_before: str,
    ) -> bool:
        """True iff at least one triple with these (s,p,o) has valid_to ≤ given."""
        async with self._lock:
            return await asyncio.to_thread(
                self._invalidation_applied,
                subject, predicate, object, ended_at_or_before,
            )

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying SQLite connection (call on shutdown)."""
        try:
            self._inner.close()
        except Exception as exc:
            log.warning("locked_kg_close_failed", error=str(exc))

    # ── Thread-pool helpers (sync; called via asyncio.to_thread) ────────────

    def _find_by_source_and_triple(
        self, subject: str, predicate: str, object: str, source_turn_id: str
    ) -> str | None:
        sub_id = self._inner._entity_id(subject)
        obj_id = self._inner._entity_id(object)
        pred = predicate.lower().replace(" ", "_")
        row = self._inner._conn().execute(
            "SELECT id FROM triples "
            "WHERE source_drawer_id = ? AND subject = ? AND predicate = ? AND object = ?",
            (source_turn_id, sub_id, pred, obj_id),
        ).fetchone()
        return row["id"] if row else None

    def _invalidate_count(
        self, subject: str, predicate: str, object: str, ended_iso: str
    ) -> int:
        sub_id = self._inner._entity_id(subject)
        obj_id = self._inner._entity_id(object)
        pred = predicate.lower().replace(" ", "_")
        conn = self._inner._conn()
        with conn:
            cursor = conn.execute(
                "UPDATE triples SET valid_to = ? "
                "WHERE subject = ? AND predicate = ? AND object = ? "
                "AND valid_to IS NULL",
                (ended_iso, sub_id, pred, obj_id),
            )
            return cursor.rowcount

    def _invalidation_applied(
        self, subject: str, predicate: str, object: str, ended_at_or_before: str
    ) -> bool:
        sub_id = self._inner._entity_id(subject)
        obj_id = self._inner._entity_id(object)
        pred = predicate.lower().replace(" ", "_")
        row = self._inner._conn().execute(
            "SELECT 1 FROM triples "
            "WHERE subject = ? AND predicate = ? AND object = ? "
            "AND valid_to IS NOT NULL AND valid_to <= ? "
            "LIMIT 1",
            (sub_id, pred, obj_id, ended_at_or_before),
        ).fetchone()
        return row is not None

    def _query_entity_rows(
        self, name: str, as_of_iso: str, direction: str
    ) -> list[KgTripleRecord]:
        ent_id = self._inner._entity_id(name)
        conn = self._inner._conn()
        results: list[KgTripleRecord] = []
        bases = []
        if direction in {"outgoing", "both"}:
            bases.append(("subject", ent_id))
        if direction in {"incoming", "both"}:
            bases.append(("object", ent_id))
        for col, val in bases:
            rows = conn.execute(
                f"SELECT t.id, e_sub.name AS subject_name, t.predicate, "
                f"       e_obj.name AS object_name, t.valid_from, t.valid_to, "
                f"       t.confidence, t.source_drawer_id, t.adapter_name "
                f"FROM triples t "
                f"JOIN entities e_sub ON e_sub.id = t.subject "
                f"JOIN entities e_obj ON e_obj.id = t.object "
                f"WHERE t.{col} = ? "
                f"  AND (t.valid_from IS NULL OR t.valid_from <= ?) "
                f"  AND (t.valid_to   IS NULL OR t.valid_to   >  ?)",
                (val, as_of_iso, as_of_iso),
            ).fetchall()
            for r in rows:
                results.append(_row_to_record(r))
        return results

    def _query_combined_rows(
        self, names: list[str], as_of_iso: str, limit_per_entity: int
    ) -> list[KgTripleRecord]:
        if not names:
            return []
        ent_ids = [self._inner._entity_id(n) for n in names]
        placeholders = ",".join("?" * len(ent_ids))
        rows = self._inner._conn().execute(
            f"SELECT t.id, e_sub.name AS subject_name, t.predicate, "
            f"       e_obj.name AS object_name, t.valid_from, t.valid_to, "
            f"       t.confidence, t.source_drawer_id, t.adapter_name "
            f"FROM triples t "
            f"JOIN entities e_sub ON e_sub.id = t.subject "
            f"JOIN entities e_obj ON e_obj.id = t.object "
            f"WHERE (t.subject IN ({placeholders}) OR t.object IN ({placeholders})) "
            f"  AND (t.valid_from IS NULL OR t.valid_from <= ?) "
            f"  AND (t.valid_to   IS NULL OR t.valid_to   >  ?) "
            # Phase 1: confidence-first ordering so the per-entity cap keeps
            # the most authoritative facts; recency only breaks ties.
            f"ORDER BY t.confidence DESC, t.valid_from DESC, t.extracted_at DESC",
            (*ent_ids, *ent_ids, as_of_iso, as_of_iso),
        ).fetchall()

        # Cap per entity (subject). Rows arrive already sorted by confidence
        # DESC so the slice picks the highest-confidence ones.
        by_subj: dict[str, list[KgTripleRecord]] = {}
        for r in rows:
            rec = _row_to_record(r)
            by_subj.setdefault(rec.subject, []).append(rec)
        out: list[KgTripleRecord] = []
        for _name, recs in by_subj.items():
            out.extend(recs[:limit_per_entity])
        return out

    def _query_subject_rows(
        self, names: list[str], as_of_iso: str, limit_per_subject: int
    ) -> list[KgTripleRecord]:
        placeholders = ",".join("?" * len(names))
        rows = self._inner._conn().execute(
            f"SELECT t.id, e_sub.name AS subject_name, t.predicate, "
            f"       e_obj.name AS object_name, t.valid_from, t.valid_to, "
            f"       t.confidence, t.source_drawer_id, t.adapter_name "
            f"FROM triples t "
            f"JOIN entities e_sub ON e_sub.id = t.subject "
            f"JOIN entities e_obj ON e_obj.id = t.object "
            f"WHERE e_sub.name IN ({placeholders}) "
            f"  AND (t.valid_from IS NULL OR t.valid_from <= ?) "
            f"  AND (t.valid_to   IS NULL OR t.valid_to   >  ?) "
            f"ORDER BY t.confidence DESC, t.valid_from DESC, t.extracted_at DESC "
            f"LIMIT ?",
            (*names, as_of_iso, as_of_iso, limit_per_subject * len(names)),
        ).fetchall()

        by_subject: dict[str, list[KgTripleRecord]] = {}
        for row in rows:
            record = _row_to_record(row)
            by_subject.setdefault(record.subject, []).append(record)
        out: list[KgTripleRecord] = []
        for records in by_subject.values():
            out.extend(records[:limit_per_subject])
        return out

    def _timeline_rows(
        self,
        entity_name: str | None,
        since: str | None,
        until: str | None,
        limit: int,
    ) -> list[KgTripleRecord]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if entity_name:
            ent_id = self._inner._entity_id(entity_name)
            clauses.append("(t.subject = ? OR t.object = ?)")
            params.extend([ent_id, ent_id])
        if since:
            clauses.append("(t.valid_from IS NULL OR t.valid_from >= ?)")
            params.append(since)
        if until:
            clauses.append("(t.valid_from IS NULL OR t.valid_from <= ?)")
            params.append(until)
        sql = (
            "SELECT t.id, e_sub.name AS subject_name, t.predicate, "
            "       e_obj.name AS object_name, t.valid_from, t.valid_to, "
            "       t.confidence, t.source_drawer_id, t.adapter_name "
            "FROM triples t "
            "JOIN entities e_sub ON e_sub.id = t.subject "
            "JOIN entities e_obj ON e_obj.id = t.object "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY t.valid_from DESC NULLS LAST, t.extracted_at DESC "
            "LIMIT ?"
        )
        params.append(limit)
        rows = self._inner._conn().execute(sql, tuple(params)).fetchall()
        return [_row_to_record(r) for r in rows]

    def _stats(self) -> dict[str, Any]:
        conn = self._inner._conn()
        entity_count = conn.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"]
        triple_count = conn.execute("SELECT COUNT(*) AS c FROM triples").fetchone()["c"]
        active_count = conn.execute(
            "SELECT COUNT(*) AS c FROM triples WHERE valid_to IS NULL"
        ).fetchone()["c"]
        return {
            "entities": int(entity_count),
            "triples_total": int(triple_count),
            "triples_active": int(active_count),
            "triples_invalidated": int(triple_count) - int(active_count),
        }

    def _entity_names(self) -> list[str]:
        rows = self._inner._conn().execute("SELECT name FROM entities").fetchall()
        return [r["name"] for r in rows]


# ── Helpers ─────────────────────────────────────────────────────────────────


def _row_to_record(row: Any) -> KgTripleRecord:
    return KgTripleRecord(
        id=row["id"],
        subject=row["subject_name"],
        predicate=row["predicate"],
        object=row["object_name"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        confidence=row["confidence"],
        source_turn_id=row["source_drawer_id"],
        adapter_name=row["adapter_name"],
    )


def _filter_sensitive(
    rows: list[KgTripleRecord], include_sensitive: bool
) -> list[KgTripleRecord]:
    if include_sensitive:
        return rows
    return [r for r in rows if r.predicate not in SENSITIVE_PREDICATES]


def _entity_name_in_query(name: str, query: str) -> bool:
    """Decide whether canonical ``name`` "appears" in ``query``.

    1. literal substring containment(原始策略,无前缀实体走这里)
    2. type-prefix stripping("pet:铁锤" → "铁锤",再次试匹)— bridges
       the gap between steward's prefixed canonical names and natural-
       language queries from users / agents.

    Empty bare tails are guarded against (``pet:`` 这种破损名不该匹任何 query)。
    """
    if not name:
        return False
    if name in query:
        return True
    if ":" in name:
        bare = name.split(":", 1)[1]
        if bare and bare in query:
            return True
    return False
