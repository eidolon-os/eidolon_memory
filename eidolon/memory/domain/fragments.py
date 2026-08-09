"""Memory fragments produced by steward pipelines."""

from __future__ import annotations

import re
from typing import Any, Literal

from eidolon_memory_contracts import OWNER_AUDIENCE, validate_audience
from pydantic import Field, field_validator

from eidolon.memory.support.model_base import BaseEidolonModel

MemoryType = str

PrivacyLevel = Literal["normal", "sensitive", "private", "do_not_recall"]
MemoryScope = Literal["global", "persona", "agent", "device", "session"]
MemoryVisibility = Literal["all_devices", "current_device", "private"]

_EXTENSION_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def is_usable_extension(namespace: object, payload: object) -> bool:
    """Whether this namespace/payload pair can be held by ``extensions``.

    Public because the LLM steward strips what it cannot store before handing
    the decision to validation, and the two must agree on the rule. If they
    drifted, the steward would either discard entries the domain accepts or
    forward entries that still fail the whole decision — which is the failure it
    exists to prevent.
    """

    return bool(isinstance(payload, dict) and _EXTENSION_NAMESPACE_RE.fullmatch(str(namespace)))


class MemoryFragment(BaseEidolonModel):
    """A single durable memory unit ready to be written to MemPalace."""

    memory_id: str = ""
    memory_space_id: str
    memory_realm_id: str | None = None
    owner_id: str | None = None
    companion_id: str | None = None
    # Who may recall this, which is not the same question as who produced it.
    # ``companion_id`` records provenance; audience records visibility. A fact
    # about the owner holds whichever companion is listening, while what happened
    # between the owner and one companion belongs to that companion.
    #
    # Defaults to the owner layer: with one companion there is nothing to leak,
    # and defaulting narrow would instead hide the owner's own facts from their
    # other companions — the worse of the two failures. Deciding per statement
    # needs the steward to judge it.
    audience: str = OWNER_AUDIENCE
    scope: MemoryScope = "persona"
    visibility: MemoryVisibility = "all_devices"
    source_device_id: str | None = None
    target_device_id: str | None = None
    source_instance_id: str | None = None
    source_turn_id: str
    session_id: str | None = None
    wing: str
    room: str
    content: str
    memory_type: MemoryType
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    occurred_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    privacy: PrivacyLevel = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator(
        "memory_space_id",
        "source_turn_id",
        "wing",
        "room",
        "content",
    )
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            msg = "memory fragment field cannot be blank"
            raise ValueError(msg)
        return value

    @field_validator("audience")
    @classmethod
    def _known_audience(cls, value: str) -> str:
        """Reject an audience we do not recognise rather than storing it.

        The token lands in metadata keys and store filter expressions, and an
        unknown one would silently match nothing — a memory written but never
        recalled, which is worse than a rejected write.
        """

        return validate_audience(value)

    @field_validator(
        "memory_realm_id",
        "owner_id",
        "companion_id",
        "source_device_id",
        "source_instance_id",
        "session_id",
    )
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("extensions")
    @classmethod
    def _validate_extensions(
        cls,
        value: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        for namespace, payload in value.items():
            if not is_usable_extension(namespace, payload):
                msg = (
                    f"extension {namespace!r} must be a lowercase namespace with a "
                    f"dict payload, got {type(payload).__name__}"
                )
                raise ValueError(msg)
        return value
