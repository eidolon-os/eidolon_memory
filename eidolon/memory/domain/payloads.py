"""Memory RPC / JetStream message payloads (NATS envelope bodies)."""

from __future__ import annotations

from typing import Any

from eidolon_sdk.memory import ConversationTurnPayload

from eidolon.memory.support.model_base import BaseEidolonModel

__all__ = [
    "ConversationTurnPayload",
    "MemoryDeletePayload",
    "MemoryGetAllPayload",
    "MemoryGetPayload",
    "MemoryQueryPayload",
    "MemoryResultPayload",
    "MemoryStorePayload",
]


class MemoryQueryPayload(BaseEidolonModel):
    """Legacy wire shape for historical ``MEMORY_QUERY`` payloads (no longer served)."""

    query: str
    wing: str | None = None
    room: str | None = None
    n_results: int = 5
    correlation_id: str = ""
    user_id: str | None = None


class MemoryStorePayload(BaseEidolonModel):
    """Request payload for storing a memory entry."""

    text: str
    wing: str | None = None
    room: str | None = None
    user_id: str | None = None
    source_file: str | None = None
    correlation_id: str = ""
    occurred_at: str | None = None
    verbatim: bool | None = None


class MemoryResultPayload(BaseEidolonModel):
    """Response payload for both query and store operations."""

    correlation_id: str
    results: list[dict[str, Any]] = []
    error: str | None = None
    is_store: bool = False


class MemoryGetPayload(BaseEidolonModel):
    """Request payload for getting a single record by user_id and key."""

    user_id: str
    key: str
    correlation_id: str = ""


class MemoryGetAllPayload(BaseEidolonModel):
    """Request payload for getting all records for a user."""

    user_id: str
    correlation_id: str = ""


class MemoryDeletePayload(BaseEidolonModel):
    """Request payload for deleting a record."""

    user_id: str
    key: str
    correlation_id: str = ""
