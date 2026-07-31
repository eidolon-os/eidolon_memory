from __future__ import annotations

import pytest
from eidolon_memory_contracts import KG_PREDICATE_VALUES, SENSITIVE_PREDICATES

from eidolon.memory.domain.predicates import (
    PredicateCardinality,
    PredicateTemporality,
    PredicateUpdatePolicy,
    predicate_definition,
    predicate_definitions,
)


def test_registry_covers_wire_contract_exactly() -> None:
    definitions = predicate_definitions()

    assert tuple(item.predicate for item in definitions) == KG_PREDICATE_VALUES
    assert {item.predicate for item in definitions if item.sensitive} == set(
        SENSITIVE_PREDICATES
    )


def test_only_product_approved_current_slot_can_explicitly_supersede() -> None:
    residence = predicate_definition("lives_in")
    birthplace = predicate_definition("born_in")
    preference = predicate_definition("likes")

    assert residence.cardinality == PredicateCardinality.SINGLE
    assert residence.temporality == PredicateTemporality.CURRENT_STATE
    assert residence.update_policy == PredicateUpdatePolicy.SUPERSEDE_EXPLICIT
    assert birthplace.cardinality == PredicateCardinality.SINGLE
    assert birthplace.update_policy == PredicateUpdatePolicy.REQUIRE_CORRECTION
    assert preference.cardinality == PredicateCardinality.MULTI
    assert preference.update_policy == PredicateUpdatePolicy.EXACT_ONLY


def test_unknown_predicate_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported memory predicate"):
        predicate_definition("llm_invented_relation")
