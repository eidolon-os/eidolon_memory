"""Versioned memory wire envelopes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, TypeAdapter

from ._model import EidolonWireModel
from .kg import MemoryCommandPayload
from .payloads import ConversationTurnPayload

MEMORY_SCHEMA_VERSION = "eidolon.memory.v1"

_COMMAND_ADAPTER = TypeAdapter(MemoryCommandPayload)
_TURN_ADAPTER = TypeAdapter(ConversationTurnPayload)


class MemoryEnvelope(EidolonWireModel):
    """Versioned envelope for memory bus payloads."""

    schema_version: Literal["eidolon.memory.v1"] = MEMORY_SCHEMA_VERSION
    kind: str = Field(min_length=1)
    payload: dict[str, Any]
    content_type: Literal["application/json"] = "application/json"
    trace_id: str | None = None


def memory_payload_kind(payload: Any) -> str:
    """Return the stable kind for a memory payload object or dict."""
    if isinstance(payload, dict):
        kind = payload.get("kind")
    else:
        kind = getattr(payload, "kind", None)
    if isinstance(kind, str) and kind:
        return kind
    if isinstance(payload, ConversationTurnPayload):
        return "conversation_turn"
    return payload.__class__.__name__


def envelope_memory_payload(
    payload: EidolonWireModel | dict[str, Any],
    *,
    kind: str | None = None,
    trace_id: str | None = None,
) -> MemoryEnvelope:
    """Wrap a memory payload in the v1 envelope."""
    payload_data = (
        payload.model_dump(mode="json")
        if isinstance(payload, EidolonWireModel)
        else dict(payload)
    )
    return MemoryEnvelope(
        kind=kind or memory_payload_kind(payload),
        payload=payload_data,
        trace_id=trace_id,
    )


def unwrap_memory_payload(data: MemoryEnvelope | dict[str, Any]) -> dict[str, Any]:
    """Return raw payload data from a versioned memory envelope."""
    if isinstance(data, MemoryEnvelope):
        return dict(data.payload)
    if data.get("schema_version") == MEMORY_SCHEMA_VERSION and isinstance(
        data.get("payload"), dict
    ):
        return dict(data["payload"])
    raise ValueError("memory payload must use the versioned envelope")


def parse_conversation_turn(data: MemoryEnvelope | dict[str, Any]) -> ConversationTurnPayload:
    """Validate a conversation-turn payload from versioned wire data."""
    return _TURN_ADAPTER.validate_python(unwrap_memory_payload(data))


def parse_memory_command(data: MemoryEnvelope | dict[str, Any]) -> MemoryCommandPayload:
    """Validate a memory command payload from versioned wire data."""
    return _COMMAND_ADAPTER.validate_python(unwrap_memory_payload(data))
