"""Memory command payload contracts for JetStream writes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ._model import EidolonWireModel
from .intent import MemoryIntent
from .subjects import validate_memory_space_id

KgPredicate = Literal[
    "child_of",
    "parent_of",
    "partner_of",
    "sibling_of",
    "friend_of",
    "colleague_of",
    "works_at",
    "lives_in",
    "studies_at",
    "holds_role",
    "born_in",
    "likes",
    "dislikes",
    "prefers",
    "does",
    "practices",
    "owns",
    "uses",
    "promised",
    "committed_to",
    "planned_to",
    "has_state",
    "has_emotion",
    "has_concern",
    "worried_about",
    "struggles_with",
    "has_health_condition",
    "takes_medication",
    "has_symptom",
    "attended",
    "experienced",
    "achieved",
]

SENSITIVE_PREDICATES: frozenset[str] = frozenset(
    {"has_health_condition", "takes_medication", "has_symptom"}
)

KG_PREDICATE_VALUES: tuple[str, ...] = tuple(
    getattr(KgPredicate, "__args__", ())  # type: ignore[attr-defined]
)


class _BaseMemoryCommand(EidolonWireModel):
    """Common fields on every command published to ``eidolon.memory.cmd.<space_token>``."""

    request_id: str
    memory_space_id: str
    issued_at: str
    issuer: Literal["admin", "agent"] = "admin"

    @field_validator("memory_space_id")
    @classmethod
    def _valid_memory_space_id(cls, value: str) -> str:
        return validate_memory_space_id(value)


class KgAddTripleCommand(_BaseMemoryCommand):
    kind: Literal["kg_add_triple"] = "kg_add_triple"
    subject: str
    predicate: KgPredicate
    object: str
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    source_drawer_id: str | None = None
    adapter_name: str = "admin"


class KgInvalidateCommand(_BaseMemoryCommand):
    kind: Literal["kg_invalidate"] = "kg_invalidate"
    subject: str
    predicate: KgPredicate
    object: str
    ended: str | None = None


class ConsolidatorIngestThemeCommand(_BaseMemoryCommand):
    kind: Literal["consolidator_ingest_theme"] = "consolidator_ingest_theme"
    text: str = Field(min_length=1)
    underlying_wing: str
    window_days: int = Field(gt=0, default=30)
    source_drawer_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.7)


class MemoryIntentCommand(_BaseMemoryCommand):
    """Submit one explicit business intent through the Realm command stream."""

    kind: Literal["memory_intent"] = "memory_intent"
    intent: MemoryIntent

    @model_validator(mode="after")
    def _same_memory_space(self) -> MemoryIntentCommand:
        if self.intent.memory_space_id != self.memory_space_id:
            raise ValueError("intent memory_space_id must match command memory_space_id")
        return self


class PrivacyMutationCommand(_BaseMemoryCommand):
    """Apply an explicitly confirmed privacy mutation to exact drawer IDs."""

    kind: Literal["privacy_mutation"] = "privacy_mutation"
    action: Literal["archive", "delete"]
    drawer_ids: list[str] = Field(min_length=1, max_length=100)
    preview_id: str = Field(min_length=1)
    target: str = ""

    @field_validator("drawer_ids")
    @classmethod
    def _valid_drawer_ids(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned or any(not value.startswith("drawer_") for value in cleaned):
            raise ValueError("drawer_ids must contain MemPalace drawer IDs")
        return cleaned


class DeviceSyncEvent(EidolonWireModel):
    """One offline memory event replayed from a device outbox."""

    event_id: str = Field(min_length=1)
    idempotency_hash: str = Field(min_length=1)
    turn: dict[str, Any]


class DeviceSyncBatchPayload(_BaseMemoryCommand):
    kind: Literal["device_sync_batch"] = "device_sync_batch"
    device_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    events: list[DeviceSyncEvent] = Field(default_factory=list)


USER_CONFIRMED_ROOM_PREFIX = "userconfirm:"

MemoryCommandPayload = (
    KgAddTripleCommand
    | KgInvalidateCommand
    | ConsolidatorIngestThemeCommand
    | MemoryIntentCommand
    | PrivacyMutationCommand
    | DeviceSyncBatchPayload
)
"""Discriminated union; route on the ``kind`` field."""
