import pytest
from pydantic import ValidationError

from eidolon_memory_contracts import (
    ConversationTurnPayload,
    MemoryActorContext,
    build_memory_actor_context,
    envelope_memory_payload,
    parse_conversation_turn,
)


def test_person_identity_survives_event_roundtrip():
    ctx = build_memory_actor_context(
        memory_realm_id="realm-a",
        person_id="person-a",
        identity_epoch=0,
        session_id="s",
        companion_id="c",
    )
    turn = ConversationTurnPayload(
        turn_id="t",
        context=ctx,
        user_text="hi",
        assistant_text="hello",
        timestamp="2026-10-27T00:00:00Z",
    )
    parsed = parse_conversation_turn(envelope_memory_payload(turn))
    assert parsed.context.identity_binding() == ctx.identity_binding()
    assert parsed.context.memory_space_id == "realm-a"
    assert parsed.context.owner_id is None


@pytest.mark.parametrize(
    "extra",
    [
        {"person_id": "p"},
        {"identity_epoch": 0},
        {"person_id": "p", "identity_epoch": -1},
        {"person_id": "p", "identity_epoch": True},
        {"person_id": "p", "identity_epoch": "1"},
        {"person_id": " ", "identity_epoch": 1},
        {"person_id": "p", "identity_epoch": 1, "session_id": None},
    ],
)
def test_incomplete_or_invalid_person_binding_is_rejected(extra):
    with pytest.raises(ValidationError):
        MemoryActorContext.model_validate({"memory_realm_id": "r", "session_id": "s", **extra})


def test_legacy_actor_remains_explicitly_unbound():
    ctx = MemoryActorContext(memory_realm_id="r")
    assert ctx.identity_binding()["person_id"] is None
    assert ctx.identity_binding()["identity_epoch"] is None
