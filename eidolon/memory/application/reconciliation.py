"""Deterministic, side-effect-free planning for structured memory intents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from eidolon_sdk.memory import MemoryIntent

from eidolon.memory.domain.kg import KgTripleRecord

ReconciliationOperation = Literal["add", "noop", "update", "conflict"]
PredicateCardinality = Literal["single", "multi"]


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    operation: ReconciliationOperation
    existing_triple_ids: tuple[str, ...] = ()
    existing_objects: tuple[str, ...] = ()
    reason: str = ""


def plan_structured_intent(
    intent: MemoryIntent,
    current: list[KgTripleRecord],
    *,
    cardinality: PredicateCardinality,
) -> ReconciliationPlan:
    """Classify a claim using reviewed predicate policy, without side effects."""
    if not intent.subject or not intent.predicate or not intent.object:
        raise ValueError("structured reconciliation requires a complete triple")

    slot = [
        row
        for row in current
        if row.subject == intent.subject and row.predicate == intent.predicate
    ]
    exact = [row for row in slot if row.object == intent.object]
    if exact:
        return ReconciliationPlan(
            operation="noop",
            existing_triple_ids=tuple(row.id for row in exact),
            existing_objects=tuple(row.object for row in exact),
            reason="an active exact fact already exists",
        )

    if cardinality == "multi" or not slot:
        return ReconciliationPlan(
            operation="add",
            reason="no active exact fact occupies this reviewed slot",
        )

    if len(slot) == 1:
        row = slot[0]
        return ReconciliationPlan(
            operation="update",
            existing_triple_ids=(row.id,),
            existing_objects=(row.object,),
            reason="one active value occupies a single-valued slot",
        )

    return ReconciliationPlan(
        operation="conflict",
        existing_triple_ids=tuple(row.id for row in slot),
        existing_objects=tuple(row.object for row in slot),
        reason="multiple active values occupy a single-valued slot",
    )
