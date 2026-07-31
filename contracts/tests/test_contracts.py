from __future__ import annotations

import pytest
from pydantic import ValidationError

from eidolon_memory_contracts import (
    KG_PREDICATE_VALUES,
    MEMORY_SCHEMA_VERSION,
    SENSITIVE_PREDICATES,
    ConversationTurnPayload,
    KgAddTripleCommand,
    PrivacyMutationCommand,
    build_memory_actor_context,
    conversation_turn_subject,
    envelope_memory_payload,
    memory_sync_subject,
    memory_command_subject,
    memory_space_storage_name,
    memory_space_subject_token,
    parse_conversation_turn,
    parse_memory_command,
    unwrap_memory_payload,
)
from eidolon_memory_contracts.subjects import all_memory_stream_patterns


def test_memory_subjects_are_stable() -> None:
    memory_space_id = "realm:owner-a:default"
    token = "b64_cmVhbG06b3duZXItYTpkZWZhdWx0"
    assert memory_space_subject_token(memory_space_id) == token
    assert memory_space_storage_name(memory_space_id) == token
    assert "." not in token
    assert ":" not in token
    assert "/" not in token
    assert conversation_turn_subject(memory_space_id) == (f"eidolon.memory.turn.{token}")
    assert memory_command_subject(memory_space_id) == (f"eidolon.memory.cmd.{token}")
    assert memory_sync_subject(memory_space_id) == (f"eidolon.memory.sync.{token}")
    assert all_memory_stream_patterns() == [
        "eidolon.memory.turn.*",
        "eidolon.memory.cmd.*",
        "eidolon.memory.sync.*",
    ]


def test_memory_subject_memory_space_id_validation() -> None:
    assert conversation_turn_subject("realm.owner_01.mochi-test") == (
        "eidolon.memory.turn.b64_cmVhbG0ub3duZXJfMDEubW9jaGktdGVzdA"
    )
    with pytest.raises(ValueError):
        conversation_turn_subject("bad/user")
    with pytest.raises(ValueError):
        conversation_turn_subject("_bad")
    with pytest.raises(ValueError):
        conversation_turn_subject("")
    with pytest.raises(ValueError):
        memory_command_subject("")


def _ctx():
    return build_memory_actor_context(
        owner_id="owner-a",
        companion_id="companion-a",
        memory_realm_id="realm:owner-a:default",
        device_id="device-1",
        session_id="s1",
    )


def test_build_memory_actor_context_uses_memory_realm_as_space() -> None:
    ctx = _ctx()

    assert ctx.owner_id == "owner-a"
    assert ctx.companion_id == "companion-a"
    assert ctx.memory_realm_id == "realm:owner-a:default"
    assert ctx.memory_space_id == "realm:owner-a:default"


def test_memory_actor_context_accepts_realm_only() -> None:
    ctx = build_memory_actor_context(memory_realm_id="realm:owner-a:default")

    assert ctx.memory_realm_id == "realm:owner-a:default"
    assert ctx.memory_space_id == "realm:owner-a:default"
    assert ctx.owner_id is None
    assert ctx.companion_id is None
    assert ctx.device_id is None
    assert ctx.session_id is None


def test_conversation_turn_payload_serializes_wire_shape() -> None:
    payload = ConversationTurnPayload(
        turn_id="t1",
        context=_ctx(),
        timestamp="2026-06-15T00:00:00Z",
        user_text="hi",
        assistant_text="hello",
        metadata={"source": "test"},
    )

    assert payload.model_dump(mode="json") == {
        "turn_id": "t1",
        "context": {
            "owner_id": "owner-a",
            "companion_id": "companion-a",
            "memory_realm_id": "realm:owner-a:default",
            "device_id": "device-1",
            "session_id": "s1",
            "memory_space_id": "realm:owner-a:default",
        },
        "timestamp": "2026-06-15T00:00:00Z",
        "user_text": "hi",
        "assistant_text": "hello",
        "metadata": {"source": "test"},
    }


def test_kg_command_predicate_contract() -> None:
    assert "likes" in KG_PREDICATE_VALUES
    assert SENSITIVE_PREDICATES <= set(KG_PREDICATE_VALUES)

    cmd = KgAddTripleCommand(
        request_id="req-1",
        memory_space_id="realm:owner-a:default",
        issued_at="2026-06-15T00:00:00Z",
        subject="Alice",
        predicate="likes",
        object="tea",
    )
    assert cmd.kind == "kg_add_triple"

    with pytest.raises(ValidationError):
        KgAddTripleCommand(
            request_id="req-2",
            memory_space_id="realm:owner-a:default",
            issued_at="2026-06-15T00:00:00Z",
            subject="Alice",
            predicate="invented_relation",
            object="tea",
        )


def test_privacy_mutation_command_is_exact_id_only() -> None:
    command = PrivacyMutationCommand(
        request_id="privacy-1",
        memory_space_id="realm:owner-a:default",
        issued_at="2026-06-15T00:00:00Z",
        action="delete",
        drawer_ids=["drawer_a", "drawer_b", "drawer_a"],
        preview_id="preview-1",
    )
    parsed = parse_memory_command(envelope_memory_payload(command))

    assert isinstance(parsed, PrivacyMutationCommand)
    assert parsed.drawer_ids == ["drawer_a", "drawer_b"]
    with pytest.raises(ValidationError, match="drawer_ids"):
        PrivacyMutationCommand(
            request_id="privacy-2",
            memory_space_id="realm:owner-a:default",
            issued_at="2026-06-15T00:00:00Z",
            action="delete",
            drawer_ids=["natural language target"],
            preview_id="preview-2",
        )


def test_memory_envelope_wraps_and_parses_conversation_turn() -> None:
    payload = ConversationTurnPayload(
        turn_id="t1",
        context=_ctx(),
        timestamp="2026-06-15T00:00:00Z",
        user_text="hi",
        assistant_text="hello",
    )

    envelope = envelope_memory_payload(payload, trace_id="trace-1")

    assert envelope.schema_version == MEMORY_SCHEMA_VERSION
    assert envelope.kind == "conversation_turn"
    assert envelope.trace_id == "trace-1"
    assert unwrap_memory_payload(envelope) == payload.model_dump(mode="json")
    assert parse_conversation_turn(envelope) == payload


def test_memory_parsers_require_versioned_envelope() -> None:
    raw_turn = {
        "turn_id": "t1",
        "context": _ctx().model_dump(mode="json"),
        "timestamp": "2026-06-15T00:00:00Z",
        "user_text": "hi",
        "assistant_text": "hello",
    }
    raw_command = {
        "kind": "kg_add_triple",
        "request_id": "req-1",
        "memory_space_id": "realm:owner-a:default",
        "issued_at": "2026-06-15T00:00:00Z",
        "subject": "Alice",
        "predicate": "likes",
        "object": "tea",
    }

    with pytest.raises(ValueError, match="versioned envelope"):
        parse_conversation_turn(raw_turn)
    with pytest.raises(ValueError, match="versioned envelope"):
        parse_memory_command(raw_command)
