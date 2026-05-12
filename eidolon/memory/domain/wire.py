"""Wire DTOs for the memory package (no dependency on ``eidolon.agent.context``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MemoryWireRecord(BaseModel):
    """Wire shape aligned with ``MemoryRecord`` for NATS / JSON payloads."""

    user_id: str
    key: str
    value: Any
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_result_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
