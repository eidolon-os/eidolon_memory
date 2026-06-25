"""Domain schemas for the KG plan v1.1: predicate whitelist, action models."""

from __future__ import annotations

import pytest
from eidolon_sdk.memory import (
    KG_PREDICATE_VALUES,
    SENSITIVE_PREDICATES,
    KgAddTripleCommand,
    KgInvalidateCommand,
    MemoryCommandPayload,
)
from pydantic import ValidationError

from eidolon.memory.domain.kg import (
    KgInvalidationAction,
    KgTripleAction,
)


def test_predicate_whitelist_size_known() -> None:
    # Snapshot to catch accidental predicate adds (every new predicate should
    # be a deliberate plan update, not a casual import).
    assert 27 <= len(KG_PREDICATE_VALUES) <= 32, (
        f"got {len(KG_PREDICATE_VALUES)} predicates"
    )


def test_sensitive_predicates_subset_of_whitelist() -> None:
    assert SENSITIVE_PREDICATES <= set(KG_PREDICATE_VALUES)
    assert "has_health_condition" in SENSITIVE_PREDICATES


def test_kg_triple_action_accepts_whitelisted_predicate() -> None:
    t = KgTripleAction(subject="self", predicate="likes", object="coffee")
    assert t.predicate == "likes"
    assert t.confidence == 0.9


def test_kg_triple_action_rejects_unknown_predicate() -> None:
    with pytest.raises(ValidationError):
        KgTripleAction(subject="self", predicate="loves", object="coffee")  # type: ignore[arg-type]


def test_kg_triple_action_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        KgTripleAction(subject="self", predicate="likes", object="x", confidence=1.5)
    with pytest.raises(ValidationError):
        KgTripleAction(subject="self", predicate="likes", object="x", confidence=-0.1)


def test_kg_triple_action_rejects_empty_subject() -> None:
    with pytest.raises(ValidationError):
        KgTripleAction(subject="", predicate="likes", object="coffee")


def test_kg_invalidation_action_basic() -> None:
    inv = KgInvalidationAction(subject="self", predicate="likes", object="coffee")
    assert inv.ended is None
    assert inv.reason == ""


def test_command_discriminator_round_trip_add() -> None:
    payload = {
        "kind": "kg_add_triple",
        "request_id": "abc123",
        "memory_space_id": "default.alice.default",
        "issued_at": "2026-05-19T10:00:00+00:00",
        "subject": "self",
        "predicate": "likes",
        "object": "coffee",
    }
    cmd = KgAddTripleCommand.model_validate(payload)
    assert cmd.kind == "kg_add_triple"
    assert cmd.issuer == "admin"  # default


def test_command_discriminator_round_trip_invalidate() -> None:
    payload = {
        "kind": "kg_invalidate",
        "request_id": "def456",
        "memory_space_id": "default.alice.default",
        "issued_at": "2026-05-19T11:00:00+00:00",
        "subject": "self",
        "predicate": "likes",
        "object": "coffee",
    }
    cmd = KgInvalidateCommand.model_validate(payload)
    assert cmd.kind == "kg_invalidate"


def test_memory_command_payload_union_routing() -> None:
    add: MemoryCommandPayload = KgAddTripleCommand(
        request_id="r1",
        memory_space_id="default.alice.default",
        issued_at="2026-05-19T10:00:00+00:00",
        subject="self",
        predicate="likes",
        object="coffee",
    )
    assert isinstance(add, KgAddTripleCommand)
    assert add.kind == "kg_add_triple"
