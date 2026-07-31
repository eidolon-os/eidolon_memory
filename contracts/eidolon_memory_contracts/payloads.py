"""Memory JetStream payload contracts."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from ._model import EidolonWireModel
from .subjects import derive_memory_space_id, validate_memory_space_id


class MemoryActorContext(EidolonWireModel):
    """Resolved actor context carried by every memory write and recall."""

    owner_id: str | None = None
    companion_id: str | None = None
    memory_realm_id: str
    device_id: str | None = None
    session_id: str | None = None
    memory_space_id: str = ""

    @field_validator("memory_realm_id")
    @classmethod
    def _realm_not_blank(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("memory_realm_id cannot be blank")
        return text

    @field_validator("owner_id", "companion_id", "device_id", "session_id")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def _fill_memory_space_id(self) -> MemoryActorContext:
        expected = derive_memory_space_id(self.memory_realm_id)
        if self.memory_space_id:
            actual = validate_memory_space_id(self.memory_space_id)
            if self.memory_space_id != expected:
                raise ValueError(
                    "memory_space_id must equal memory_realm_id"
                )
            object.__setattr__(self, "memory_space_id", actual)
        else:
            object.__setattr__(self, "memory_space_id", expected)
        return self


class ConversationTurnPayload(EidolonWireModel):
    """One completed user/assistant turn published to memory."""

    turn_id: str
    context: MemoryActorContext
    user_text: str
    assistant_text: str
    timestamp: str
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_memory_actor_context(
    *,
    memory_realm_id: str,
    owner_id: str | None = None,
    companion_id: str | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
) -> MemoryActorContext:
    """Build the canonical memory actor context from realm identity."""

    return MemoryActorContext(
        owner_id=owner_id,
        companion_id=companion_id,
        memory_realm_id=memory_realm_id,
        device_id=device_id,
        session_id=session_id,
    )
