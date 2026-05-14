"""Domain layer: wire DTOs, message payloads, fragments, and backend ports."""

from eidolon.memory.domain.errors import (
    MemoryBackendError,
    MemoryBackendUnavailable,
    MemoryBackendUnsupported,
    MemoryBackendWriteFailed,
    StewardError,
    StewardOutputError,
)
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.payloads import (
    ConversationTurnPayload,
    MemoryDeletePayload,
    MemoryGetAllPayload,
    MemoryGetPayload,
    MemoryQueryPayload,
    MemoryResultPayload,
    MemoryStorePayload,
)
from eidolon.memory.domain.ports import MemoryAdmin, MemoryBackend, MemoryReader, MemoryWriter
from eidolon.memory.domain.steward import PrivacyAction, StewardDecision
from eidolon.memory.domain.wire import MemoryWireRecord

__all__ = [
    "MemoryAdmin",
    "MemoryBackend",
    "MemoryBackendError",
    "MemoryBackendUnavailable",
    "MemoryBackendUnsupported",
    "MemoryBackendWriteFailed",
    "MemoryFragment",
    "MemoryReader",
    "MemoryWriter",
    "PrivacyAction",
    "StewardDecision",
    "StewardError",
    "StewardOutputError",
    "MemoryWireRecord",
    "ConversationTurnPayload",
    "MemoryQueryPayload",
    "MemoryStorePayload",
    "MemoryResultPayload",
    "MemoryGetPayload",
    "MemoryGetAllPayload",
    "MemoryDeletePayload",
]
