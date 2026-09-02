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

    #: Text that identifies the claim. Blank is a malformed intent, not a
    #: missing one, so it stays fatal — an intent with no id or no claim has
    #: nothing for a consumer to reconcile.
    @field_validator("intent_id", "source_event_id", "raw_claim")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("memory intent text fields cannot be blank")
        return text

    @field_validator("target_id", "subject", "predicate", "object", "occurred_at", "tool_call_id")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        """Blank means absent, because absent is already what these declare.

        Every field here is ``str | None`` — ``None`` is a legal value and the
        pipeline is built for it. ``occurred_at`` is the clearest case: when it
        arrives as ``None`` the turn processor fills it with the turn's own
        timestamp, which it has. When it arrived as ``""`` this validator
        raised, and because validation is all-or-nothing the entire decision —
        every fragment and every triple extracted from that turn — was
        discarded and the turn was retried from the durable stream.

        So one field had two spellings of "I have no timestamp": one repaired,
        one fatal, with nothing telling a model which to use. Measured on
        2026-09-01 across two 40-turn benchmark ingests, this was the dominant
        extraction failure — 4 of 4 discarded decisions in one run and 4 of 5
        in the other — and it is the same shape as the ``memory_space_id`` and
        ``source_turn_id`` cases already recorded in the steward: a field that
        is not the substance of a memory deciding whether the memory exists.

        Refusing blank never protected a consumer here, because ``None`` was
        always going to reach the same code. It only converted missing metadata
        into a lost turn.
        """

        if value is None:
            return None
        return value.strip() or None
