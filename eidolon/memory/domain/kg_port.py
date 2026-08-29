"""The knowledge graph the service actually needs.

MemPalace supplies a graph, but hardcoded to one SQLite file with no backend
concept — which is the one part of a palace that cannot stay on local disk when
several hosts serve the same space. It is also the part of MemPalace with the
least contract around it, so depending on it couples us to its least stable
surface.

What we need instead is small enough to own. Every graph read in this service is
one hop: given a subject, what is true about it. There is no traversal, no path
finding, no query language, no graph algorithm — the workload is triples with
validity intervals and equality lookups, which any relational store does well.
Owning the port means the graph can live in a file locally and in a shared
database in a cluster, from one set of SQL shapes.

Two axes the schema carries from the start rather than bolting on:

*Bitemporality.* A statement has a validity interval — when it was true of the
world — separate from when we recorded it. Invalidating writes an end to the
interval rather than deleting the row, so "they used to live in Berlin" survives
learning that they moved.

*Audience.* A statement is either about the owner, and so true whichever
companion is listening, or about the owner and one companion together. Filtering
on it is not an afterthought at the call site; it is a column and part of every
read.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from eidolon_memory_contracts import OWNER_AUDIENCE

from eidolon.memory.domain.kg import KgTripleRecord


@runtime_checkable
class KnowledgeGraphPort(Protocol):
    """Statements about entities, with validity intervals and an audience.

    Implementations serialise their own access. The local one shares a lock with
    the vector store so a turn's writes to both stay coherent; a database-backed
    one relies on the server and exposes ``lock = None``. Callers must not reach
    for the lock themselves.
    """

    # None on implementations whose store handles concurrency. Present so the
    # turn path can share one critical section with embedded storage.
    lock: Any

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
        assertion_id: str | None = None,
        evidence_id: str | None = None,
        projection_id: str | None = None,
        adapter_name: str | None = None,
        sensitive: bool | None = None,
    ) -> str:
        """Record a statement, returning its id. Idempotent.

        Two things make a repeat a no-op rather than a duplicate. A statement
        already recorded from the same ``source_turn_id`` returns its existing
        id, which is what makes replaying a turn safe. And an identical statement
        that is still valid is not recorded twice.

        Those two together also handle the awkward case: add, invalidate, then
        replay the original turn. The source check fires first, so the
        invalidation is not silently undone.

        ``sensitive`` defaults to whatever the predicate implies; pass it only to
        override that.
        """
        ...

    async def invalidate(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        audiences: tuple[str, ...] = (OWNER_AUDIENCE,),
        ended: str | None = None,
    ) -> int:
        """End a statement's validity, returning how many rows changed.

        Zero means nothing matching was still valid — which is a legitimate
        outcome, not a failure. Nothing is deleted: the row keeps its interval so
        the history remains answerable.
        """
        ...

    async def forget_source_turns(
        self,
        turn_ids: Sequence[str],
        *,
        hard: bool = False,
        ended: str | None = None,
    ) -> int:
        """Forget everything a set of conversation turns put in the graph.

        The graph half of an explicit "forget that" — the *only* deletion in this
        port driven by a person rather than by a correction, which is why it is
        addressed by turn and not by triple. A user who asks to be forgotten does
        not know a subject-predicate-object; they know what they said. The turn is
        also the only identity the vector store and the graph share, so it is the
        widest thing this can honour without guessing.

        Until 2026-08-06 this did not exist and the vector half ran alone: the
        drawer went and the triples stayed, and stayed in every later prompt. A
        product that answers "yes" and then keeps the fact is worse than one that
        cannot forget at all, because only one of the two is a lie.

        ``hard`` distinguishes the two things a person can mean. False ends the
        statements' validity — they stop being recalled and the history stays
        answerable, matching what archiving does on the vector side. True removes
        the rows, matching a delete: for a privacy request, "no longer returned"
        and "no longer present" are not the same promise, and an object name still
        sitting in a column is still the thing that was asked about.

        A hard forget must be recoverable by an operator even though it is not
        recoverable by the product — implementations write the rows out before
        removing them, and refuse rather than delete unrecorded. Returns the
        number of statements affected; zero is a legitimate answer for a turn that
        produced no triples.
        """
        ...

    async def forget_assertions(
        self,
        assertion_ids: Sequence[str],
        *,
        hard: bool = False,
        ended: str | None = None,
    ) -> int:
        """Forget every projection of stable canonical assertions."""
        ...

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
        """Replace one statement with another at a single instant.

        The old interval ends exactly where the new one begins, so no point in
        time has both or neither. Doing this as an invalidate followed by an add
        leaves a window where a reader sees the fact as absent.
        """
        ...

    async def record_entity_mention(
        self,
        *,
        entity_id: str,
        alias: str,
        source: str,
        audience: str = OWNER_AUDIENCE,
        confidence: float = 0.85,
    ) -> None:
        """Note that an entity was referred to by this alias.

        What makes "my dad" resolve to the same entity as his name.

        ``entity_id`` is normalised the same way the triple writes normalise
        their subject and object, so callers may pass either a display name or a
        slug. That is stated because it was not true until 2026-08-06: this was
        the one write that stored the argument verbatim, and the read joins
        mentions to entities on that column, so a caller passing a display name
        wrote a row nothing could ever reach.
        """
        ...

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
        """Statements touching an entity, as of a point in time.

        ``direction`` selects whether the entity is the subject, the object, or
        either. ``audiences`` is the set the caller may see and is never
        optional — a reader that forgot it would see another companion's private
        statements.
        """
        ...

    async def query_subjects(
        self,
        names: list[str],
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        include_sensitive: bool = False,
        limit_per_subject: int = 8,
    ) -> list[KgTripleRecord]:
        """Outgoing statements for named subjects, bounded per subject.

        The recall path's read. Bounded per subject rather than overall so one
        well-connected entity cannot crowd the others out.
        """
        ...

    async def query_entity_combined(
        self,
        names: list[str],
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        include_sensitive: bool = False,
        limit_per_entity: int = 8,
    ) -> list[KgTripleRecord]:
        """Statements in either direction for several entities, bounded each."""
        ...

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
        """Statements ordered by when they became true. For operators.

        The one read that returns ended statements — that is what a timeline is
        for. ``current_only`` narrows to open ones, and belongs here rather than in
        the caller: applied to a ``LIMIT``-ed page it silently returns fewer than
        asked for, and the caller cannot tell the difference between "that is all
        there is" and "the page was mostly history".
        """
        ...

    async def entities_for_source_turns(
        self, turn_ids: Sequence[str], *, cap: int
    ) -> list[str]:
        """Which entities the given turns produced statements about.

        The other way into the graph, and the one that works when the phrase
        names nobody. ``match_entities_for_query`` needs the person to say a name;
        "她住哪儿" and "我上次说的那个事" say none, and those are the turns where a
        graph has the most to add — so seeding only from the phrase meant the
        graph was quietest exactly when it was most useful.

        Seeded from what vector search already decided is relevant, this needs no
        matching at all: the drawers carry ``source_turn_id``, the statements are
        indexed by it, and the join is exact. No fuzzy step, no false positives.

        Returns display names, ordered most-connected first, so a caller taking
        the first few gets the entities the turns were actually about rather than
        an arbitrary slice.
        """
        ...

    async def match_entities_for_query(
        self,
        query: str,
        *,
        cap: int,
        audiences: tuple[str, ...] = (OWNER_AUDIENCE,),
    ) -> list[str]:
        """Guess which entities a piece of natural language is about.

        Bridges free text to canonical entity names, via stored aliases and
        prefix handling. Best-effort by nature — callers treat an empty result as
        "no graph contribution", never as an error.
        """
        ...

    async def known_audiences(self) -> list[str]:
        """Which audiences this space's graph contains.

        For operator tools that inspect a space in full. Enumerating rather than
        offering a wildcard is deliberate — a "match any" token would be a way
        past the filter that keeps one companion's statements out of another's
        recall.
        """
        ...

    async def list_entity_names(self) -> list[str]: ...

    async def stats(self) -> dict[str, Any]:
        """Counts for operators. Never part of recall."""
        ...

    # ── idempotency probes ──────────────────────────────────────────────────
    #
    # The MCP write tools poll for a terminal outcome, which needs a way to ask
    # "did this land?" without attempting the write again.

    async def has_triple(self, triple_id: str) -> bool: ...

    async def find_pending_triple_id(
        self,
        source_turn_id: str,
        subject: str,
        predicate: str,
        object: str,
    ) -> str | None: ...

    async def find_invalidation_applied(
        self,
        subject: str,
        predicate: str,
        object: str,
        ended_at_or_before: str,
    ) -> bool: ...

    def close(self) -> None:
        """Release resources. Idempotent."""
        ...
