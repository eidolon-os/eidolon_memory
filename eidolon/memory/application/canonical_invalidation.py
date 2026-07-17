"""Exact canonical invalidation through existing drawer and KG write ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eidolon_sdk.memory import MemoryIntent

from eidolon.memory.domain.canonical_fact import canonical_assertion_id
from eidolon.memory.domain.ports import CanonicalFactWriter, MemoryBackend


@dataclass(frozen=True, slots=True)
class ExactInvalidationResult:
    assertion_id: str
    drawer_archived: bool
    kg_rows_invalidated: int
    canonical_matched: bool


async def invalidate_exact_canonical_fact(
    backend: MemoryBackend,
    kg: Any,
    intent: MemoryIntent,
    canonical_facts: CanonicalFactWriter,
) -> ExactInvalidationResult:
    """Invalidate one complete triple without predicate-cardinality inference.

    The source assertion id identifies the optional canonical drawer exactly;
    no semantic search or substring selection participates in this mutation.
    Projection writes finish before the ledger records the historical state so
    a failed mutation never advertises a completed canonical invalidation.
    """
    if (
        intent.intent_type != "correction"
        or intent.operation_hint != "invalidate"
        or not intent.subject
        or not intent.predicate
        or not intent.object
    ):
        raise ValueError("exact canonical invalidation requires a complete triple")

    assertion_id = canonical_assertion_id(
        intent.memory_space_id,
        intent.subject,
        intent.predicate,
        intent.object,
    )
    registration = await canonical_facts.register_invalidation(intent)

    drawer = await backend.get_by_source_turn_id(
        intent.memory_space_id,
        f"canonical:{assertion_id}",
    )
    drawer_archived = False
    if drawer is not None and str(drawer.metadata.get("privacy")) != "do_not_recall":
        archived = await backend.archive_many(
            intent.memory_space_id,
            [drawer.key],
        )
        if drawer.key not in archived:
            raise RuntimeError("canonical drawer archive was not verified")
        drawer_archived = True

    kg_rows = await kg.invalidate(
        subject=intent.subject,
        predicate=intent.predicate,
        object=intent.object,
        ended=intent.occurred_at,
    )
    if registration.matched:
        await canonical_facts.mark_invalidated(
            intent.memory_space_id,
            intent.intent_id,
        )
    return ExactInvalidationResult(
        assertion_id=assertion_id,
        drawer_archived=drawer_archived,
        kg_rows_invalidated=int(kg_rows),
        canonical_matched=registration.matched,
    )
