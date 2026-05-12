"""Domain layer: wire DTOs, message payloads, and backend port."""

from eidolon.memory.domain.payloads import (
    ConversationTurnPayload,
    MemoryDeletePayload,
    MemoryGetAllPayload,
    MemoryGetPayload,
    MemoryQueryPayload,
    MemoryResultPayload,
    MemoryStorePayload,
)
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord

__all__ = [
    "MemoryBackend",
    "MemoryWireRecord",
    "ConversationTurnPayload",
    "MemoryQueryPayload",
    "MemoryStorePayload",
    "MemoryResultPayload",
    "MemoryGetPayload",
    "MemoryGetAllPayload",
    "MemoryDeletePayload",
]
