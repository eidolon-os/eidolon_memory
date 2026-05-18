"""Application layer: NATS RPC service and recall facade."""

from eidolon.memory.application.ingest import ingest_fragment, ingest_memory_fragment
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.application.livekit_recall import LiveKitRecallService
from eidolon.memory.application.recall import McpRecallClient
from eidolon.memory.application.steward import NoOpSteward

__all__ = [
    "LiveKitRecallService",
    "MemoryService",
    "McpRecallClient",
    "NoOpSteward",
    "ingest_fragment",
    "ingest_memory_fragment",
]
