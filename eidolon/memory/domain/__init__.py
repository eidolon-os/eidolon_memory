"""Domain layer: wire DTOs, message payloads, fragments, and backend ports."""

from eidolon.memory.domain.command_status import CommandStatus, CommandStatusRecord
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
    MemoryDeletePayload,
    MemoryGetAllPayload,
    MemoryGetPayload,
    MemoryQueryPayload,
    MemoryResultPayload,
    MemoryStorePayload,
)
from eidolon.memory.domain.ports import (
    CommandStatusReader,
    CommandStatusStore,
    CommandStatusWriter,
    MemoryAdmin,
    MemoryBackend,
    MemoryPrivacyAdmin,
    MemoryReader,
    MemoryWriter,
)
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
    "MemoryPrivacyAdmin",
    "CommandStatus",
    "CommandStatusRecord",
    "CommandStatusReader",
    "CommandStatusWriter",
    "CommandStatusStore",
    "PrivacyAction",
    "StewardDecision",
    "StewardError",
    "StewardOutputError",
    "MemoryWireRecord",
    "MemoryQueryPayload",
    "MemoryStorePayload",
    "MemoryResultPayload",
    "MemoryGetPayload",
    "MemoryGetAllPayload",
    "MemoryDeletePayload",
]
