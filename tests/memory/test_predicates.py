from __future__ import annotations

import ast
import inspect

import pytest
from eidolon_memory_contracts import KG_PREDICATE_VALUES, SENSITIVE_PREDICATES

from eidolon.memory.application import explicit_intents
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
    assert all(item.projection_wing for item in definitions)
    assert all(item.projection_memory_type for item in definitions)


def test_projection_semantics_live_in_the_predicate_registry() -> None:
    preference = predicate_definition("likes")
    employment = predicate_definition("works_at")
    episode = predicate_definition("attended")

    assert (
        preference.intent_type,
        preference.projection_wing,
        preference.projection_memory_type,
    ) == ("preference", "Wing_Life", "preference")
    assert (
        employment.intent_type,
        employment.projection_wing,
        employment.projection_memory_type,
    ) == ("fact", "Wing_Work", "work")
    assert (
        episode.intent_type,
        episode.projection_wing,
        episode.projection_memory_type,
    ) == ("episode", "Wing_Event", "event")


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


def test_explicit_projection_never_classifies_raw_claim_language() -> None:
    """The explicit path consumes typed semantics; it is not a second steward."""

    source = inspect.getsource(explicit_intents._projection_location)
    tree = ast.parse(source)
    accessed_attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert "raw_claim" not in accessed_attributes
