"""Memory fragments produced by steward pipelines."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator

from eidolon.memory.support.model_base import BaseEidolonModel

MemoryType = str

PrivacyLevel = Literal["normal", "sensitive", "private", "do_not_recall"]
MemoryScope = Literal["global", "persona", "agent", "device", "session"]
MemoryVisibility = Literal["all_devices", "current_device", "private"]

_EXTENSION_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class MemoryFragment(BaseEidolonModel):
    """A single durable memory unit ready to be written to MemPalace."""

    memory_id: str = ""
    memory_space_id: str
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

    @field_validator("source_device_id", "source_instance_id", "session_id")
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
            if not _EXTENSION_NAMESPACE_RE.fullmatch(namespace):
                msg = f"invalid extension namespace {namespace!r}"
                raise ValueError(msg)
            if not isinstance(payload, dict):
                msg = f"extension {namespace!r} payload must be a dict"
                raise ValueError(msg)
        return value
