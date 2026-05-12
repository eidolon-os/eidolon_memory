"""Memory package — MCP-only semantic reads; NATS/JetStream for writes and CRUD."""

from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.domain.payloads import (
    ConversationTurnPayload,
    MemoryDeletePayload,
    MemoryGetAllPayload,
    MemoryGetPayload,
    MemoryQueryPayload,
    MemoryResultPayload,
    MemoryStorePayload,
)

__all__ = [
    "MemoryService",
    "ConversationTurnPayload",
    "MemoryQueryPayload",
    "MemoryStorePayload",
    "MemoryResultPayload",
    "MemoryGetPayload",
    "MemoryGetAllPayload",
    "MemoryDeletePayload",
]
