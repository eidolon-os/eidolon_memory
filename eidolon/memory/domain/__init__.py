"""Domain layer: wire DTOs, fragments, and the ports storage must satisfy."""

from eidolon.memory.domain.command_status import (
    CommandStatus,
    CommandStatusRecord,
    CommandStatusStats,
)
from eidolon.memory.domain.dlq import DlqRecord, DlqReplayItem, DlqState, DlqStats
from eidolon.memory.domain.errors import (
    MemoryBackendError,
    MemoryBackendUnavailable,
    MemoryBackendUnsupported,
    MemoryBackendWriteFailed,
    StewardError,
    StewardOutputError,
)
from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
    extraction_input_hash,
)
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import (
    CommandStatusReader,
    CommandStatusStore,
    CommandStatusWriter,
    DlqReader,
    DlqStore,
    DlqWriter,
    ExtractionDecisionStore,
    MemoryAdmin,
    MemoryBackend,
    MemoryPrivacyAdmin,
    MemoryReader,
    MemoryWriter,
    ScopedMemoryReader,
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
    "ExtractionDecisionConflict",
    "ExtractionDecisionRecord",
    "ExtractionDecisionStore",
    "extraction_input_hash",
    "MemoryReader",
    "ScopedMemoryReader",
    "MemoryWriter",
    "MemoryPrivacyAdmin",
    "CommandStatus",
    "CommandStatusRecord",
    "CommandStatusStats",
    "CommandStatusReader",
    "CommandStatusWriter",
    "CommandStatusStore",
    "DlqState",
    "DlqRecord",
    "DlqReplayItem",
    "DlqStats",
    "DlqReader",
    "DlqWriter",
    "DlqStore",
    "PrivacyAction",
    "StewardDecision",
    "StewardError",
    "StewardOutputError",
    "MemoryWireRecord",
]
