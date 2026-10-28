"""Memory JetStream payload contracts."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from ._model import EidolonWireModel
from .subjects import derive_memory_space_id, validate_memory_space_id


class MemoryActorContext(EidolonWireModel):
    """Resolved actor context carried by every memory write and recall."""

    # Provenance supplied by a trusted caller, never an authorization grant.
    person_id: str | None = Field(default=None, min_length=1, max_length=128)
    identity_epoch: int | None = Field(default=None, ge=0, strict=True)
    owner_id: str | None = None
    companion_id: str | None = None
    council_id: str | None = None
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

    @field_validator(
        "owner_id", "companion_id", "council_id", "device_id", "session_id", "person_id"
    )
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def _fill_memory_space_id(self) -> MemoryActorContext:
        if (self.person_id is None) != (self.identity_epoch is None):
            raise ValueError("person_id and identity_epoch must be supplied together")
        if self.person_id is not None and not self.session_id:
            raise ValueError("person-bound memory requires session_id")
        expected = derive_memory_space_id(self.memory_realm_id)
        if self.memory_space_id:
            actual = validate_memory_space_id(self.memory_space_id)
            if self.memory_space_id != expected:
                raise ValueError("memory_space_id must equal memory_realm_id")
            object.__setattr__(self, "memory_space_id", actual)
        else:
            object.__setattr__(self, "memory_space_id", expected)
        return self

    def identity_binding(self) -> dict[str, Any]:
        """Echo request identity so consumers can discard stale recall results.

        This is correlation data, not proof of person-to-Realm authorization.
        """
        return self.model_dump(
            include={
                "owner_id",
                "council_id",
                "person_id",
                "identity_epoch",
                "session_id",
                "memory_realm_id",
                "memory_space_id",
                "companion_id",
                "device_id",
            }
        )


class ConversationTurnPayload(EidolonWireModel):
    """One completed user/assistant turn published to memory."""

    turn_id: str
    context: MemoryActorContext
    user_text: str
    assistant_text: str
    timestamp: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _provisional_source_requires_identity(self):
        attribution = self.metadata.get("person_attribution")
        if attribution not in (None, "confirmed", "provisional"):
            raise ValueError("invalid person attribution")
        if attribution == "provisional" and not (
            self.context.owner_id and self.context.person_id and self.context.session_id
        ):
            raise ValueError("provisional source requires Owner, Person and session")
        return self


def build_memory_actor_context(
    *,
    memory_realm_id: str,
    person_id: str | None = None,
    identity_epoch: int | None = None,
    owner_id: str | None = None,
    companion_id: str | None = None,
    council_id: str | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
) -> MemoryActorContext:
    """Build the canonical memory actor context from realm identity."""

    return MemoryActorContext(
        person_id=person_id,
        identity_epoch=identity_epoch,
        owner_id=owner_id,
        companion_id=companion_id,
        council_id=council_id,
        memory_realm_id=memory_realm_id,
        device_id=device_id,
        session_id=session_id,
    )
