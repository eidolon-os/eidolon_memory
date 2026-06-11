"""Wire DTOs for the memory package (no dependency on ``eidolon.agent.context``)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator


def parse_memory_datetime(value: Any) -> datetime | None:
    """Parse a memory timestamp, normalizing naive values to UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            text = str(value).strip()
            if not text:
                return None
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def derive_memory_time(
    metadata: dict[str, Any],
    *,
    created_at: Any = None,
    updated_at: Any = None,
) -> tuple[datetime | None, str | None]:
    """Return the canonical user-facing memory time and the source field.

    ``occurred_at`` is the preferred semantic time. Storage/index timestamps are
    only fallbacks so every caller gets one stable time point when possible.
    """
    candidates: list[tuple[str, Any]] = [
        ("memory_time", metadata.get("memory_time")),
        ("occurred_at", metadata.get("occurred_at")),
        ("valid_from", metadata.get("valid_from")),
        ("created_at", created_at if created_at is not None else metadata.get("created_at")),
        ("filed_at", metadata.get("filed_at")),
        ("updated_at", updated_at if updated_at is not None else metadata.get("updated_at")),
    ]
    for source, value in candidates:
        dt = parse_memory_datetime(value)
        if dt is not None:
            return dt, source
    return None, None


class MemoryWireRecord(BaseModel):
    """Wire shape aligned with ``MemoryRecord`` for NATS / JSON payloads."""

    user_id: str
    key: str
    value: Any
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    memory_time: datetime | None = None
    memory_time_source: str | None = None

    @model_validator(mode="after")
    def _fill_memory_time(self) -> "MemoryWireRecord":
        if self.memory_time is not None:
            if not self.memory_time_source:
                self.memory_time_source = "memory_time"
            return self
        memory_time, source = derive_memory_time(
            self.metadata,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
        self.memory_time = memory_time
        self.memory_time_source = source
        return self

    def to_result_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
