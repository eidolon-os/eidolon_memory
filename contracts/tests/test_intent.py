"""MemoryIntent is the single unversioned business contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eidolon_memory_contracts import (
    MemoryIntent,
    MemoryIntentCommand,
    envelope_memory_payload,
    parse_memory_command,
)


def _intent(**updates) -> MemoryIntent:
    values = {
        "intent_id": "turn-1:tool-1",
        "memory_space_id": "r:alice:default",
        "source_event_id": "turn-1",
        "authority": "explicit_user",
        "intent_type": "preference",
        "raw_claim": "我喜欢绿茶",
        "operation_hint": "confirm",
        "subject": "person:self",
        "predicate": "likes",
        "object": "绿茶",
        "tool_call_id": "tool-1",
        "confidence": 0.99,
    }
    values.update(updates)
    return MemoryIntent.model_validate(values)


def test_memory_intent_has_no_versioned_type_or_schema_branch() -> None:
    intent = _intent()
    assert type(intent).__name__ == "MemoryIntent"
    assert intent.source_event_id == "turn-1"
    assert "version" not in intent.model_dump()


def test_one_source_event_can_emit_multiple_independent_intents() -> None:
    preference = _intent()
    commitment = _intent(
        intent_id="turn-1:auto-2",
        authority="extracted_user",
        intent_type="commitment",
        raw_claim="以后带你去恐龙园",
        operation_hint="add",
        subject=None,
        predicate=None,
        object=None,
        tool_call_id=None,
    )

    assert preference.source_event_id == commitment.source_event_id
    assert preference.intent_id != commitment.intent_id


@pytest.mark.parametrize("field", ["intent_id", "source_event_id", "raw_claim"])
def test_required_identity_and_claim_fields_reject_blank(field: str) -> None:
    with pytest.raises(ValidationError):
        _intent(**{field: "   "})


def test_authority_and_operation_are_closed_contracts() -> None:
    with pytest.raises(ValidationError):
        _intent(authority="system_guess")
    with pytest.raises(ValidationError):
        _intent(operation_hint="supersede")


def test_memory_intent_command_is_the_only_explicit_intent_wire_shape() -> None:
    intent = _intent()
    command = MemoryIntentCommand(
        request_id="request-1",
        memory_space_id=intent.memory_space_id,
        issued_at="2026-07-16T00:00:00Z",
        issuer="agent",
        intent=intent,
    )
    parsed = parse_memory_command(envelope_memory_payload(command))
    assert isinstance(parsed, MemoryIntentCommand)
    assert parsed.intent.intent_id == intent.intent_id


def test_memory_intent_command_rejects_cross_realm_intent() -> None:
    with pytest.raises(ValidationError):
        MemoryIntentCommand(
            request_id="request-1",
            memory_space_id="r:bob:default",
            issued_at="2026-07-16T00:00:00Z",
            intent=_intent(),
        )
