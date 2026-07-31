"""Canonical memory intent shared by Agent and Memory services."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from ._model import EidolonWireModel
from .subjects import validate_memory_space_id

MemoryIntentAuthority = Literal[
    "explicit_user",
    "explicit_admin",
    "extracted_user",
    "inferred",
]
MemoryIntentType = Literal[
    "fact",
    "preference",
    "commitment",
    "episode",
    "forget",
    "correction",
]
MemoryIntentOperation = Literal["add", "update", "invalidate", "confirm"]


class MemoryIntent(EidolonWireModel):
    """One business claim emitted from a source event before reconciliation.

    ``source_event_id`` correlates explicit tool calls and automatic extraction
    from the same turn.  A source event may emit multiple independently
    identified intents; consumers must never deduplicate an entire turn merely
    because one explicit intent already exists.
    """

    intent_id: str = Field(min_length=1)
    memory_space_id: str
    source_event_id: str = Field(min_length=1)
    authority: MemoryIntentAuthority
    intent_type: MemoryIntentType
    raw_claim: str = Field(min_length=1)
    operation_hint: MemoryIntentOperation | None = None
    target_id: str | None = None
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    occurred_at: str | None = None
    tool_call_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("memory_space_id")
    @classmethod
    def _valid_memory_space_id(cls, value: str) -> str:
        return validate_memory_space_id(value)

    @field_validator(
        "intent_id",
        "source_event_id",
        "raw_claim",
        "target_id",
        "subject",
        "predicate",
        "object",
        "occurred_at",
        "tool_call_id",
    )
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("memory intent text fields cannot be blank")
        return text
