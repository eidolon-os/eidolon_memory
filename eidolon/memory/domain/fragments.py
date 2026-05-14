"""Memory fragments produced by steward pipelines."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from eidolon.memory.support.model_base import BaseEidolonModel

MemoryType = Literal[
    "profile",
    "relationship",
    "emotion",
    "event",
    "work",
    "life",
    "health",
    "preference",
    "privacy",
]

PrivacyLevel = Literal["normal", "sensitive", "private", "do_not_recall"]


class MemoryFragment(BaseEidolonModel):
    """A single durable memory unit ready to be written to MemPalace."""

    fragment_id: str = ""
    user_id: str
    wing: str
    room: str
    content: str
    memory_type: MemoryType
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    occurred_at: str | None = None
    source_turn_id: str
    session_id: str = ""
    tags: list[str] = Field(default_factory=list)
    privacy: PrivacyLevel = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("user_id", "wing", "room", "content", "source_turn_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            msg = "memory fragment field cannot be blank"
            raise ValueError(msg)
        return value
