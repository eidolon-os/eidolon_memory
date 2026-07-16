"""Minimal deterministic reconciliation policy is explicit and fail-closed."""

from __future__ import annotations

from eidolon_sdk.memory import MemoryIntent

from eidolon.memory.application.reconciliation import plan_structured_intent
from eidolon.memory.domain.kg import KgTripleRecord

MEMORY_SPACE_ID = "r:alice:default"


def _intent(*, predicate: str = "works_at", object_: str = "new") -> MemoryIntent:
    return MemoryIntent(
        intent_id="intent:1",
        memory_space_id=MEMORY_SPACE_ID,
        source_event_id="turn-1",
        authority="explicit_user",
        intent_type="fact",
        raw_claim=f"self {predicate} {object_}",
        operation_hint="confirm",
        subject="self",
        predicate=predicate,
        object=object_,
    )


def _row(id_: str, *, predicate: str = "works_at", object_: str) -> KgTripleRecord:
    return KgTripleRecord(
        id=id_,
        subject="self",
        predicate=predicate,
        object=object_,
    )


def test_exact_active_fact_is_noop_even_for_a_new_source_event() -> None:
    plan = plan_structured_intent(
        _intent(object_="常州"),
        [_row("triple-1", object_="常州")],
        cardinality="single",
    )

    assert plan.operation == "noop"
    assert plan.existing_triple_ids == ("triple-1",)


def test_unique_old_value_plans_update_for_reviewed_single_value_predicate() -> None:
    plan = plan_structured_intent(
        _intent(object_="上海"),
        [_row("triple-old", object_="常州")],
        cardinality="single",
    )

    assert plan.operation == "update"
    assert plan.existing_objects == ("常州",)


def test_multiple_old_values_are_conflict_not_guessed_update() -> None:
    plan = plan_structured_intent(
        _intent(object_="上海"),
        [
            _row("triple-1", object_="常州"),
            _row("triple-2", object_="苏州"),
        ],
        cardinality="single",
    )

    assert plan.operation == "conflict"
    assert plan.existing_triple_ids == ("triple-1", "triple-2")


def test_multi_value_predicate_adds_new_object_instead_of_superseding() -> None:
    plan = plan_structured_intent(
        _intent(predicate="likes", object_="乌龙茶"),
        [_row("triple-1", predicate="likes", object_="绿茶")],
        cardinality="multi",
    )

    assert plan.operation == "add"
