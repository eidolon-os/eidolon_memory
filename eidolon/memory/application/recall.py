"""Synchronous-style MCP recall for the LiveKit hot path (companion default)."""

from __future__ import annotations

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class McpRecallClient:
    """Thin facade over ``MemoryBackend.search`` with settings recall defaults."""

    def __init__(self, backend: MemoryBackend, settings: MemorySettings) -> None:
        self._backend = backend
        self._settings = settings

    async def recall(
        self,
        query: str,
        *,
        wing: str,
        room: str | None = None,
        top_k: int | None = None,
    ) -> list[MemoryWireRecord]:
        """Retrieve memory snippets; never raises — returns [] on failure."""
        k = top_k if top_k is not None else self._settings.recall.top_k
        try:
            return await self._backend.search(query, wing=wing, n_results=k, room=room)
        except Exception as exc:
            log.warning("mcp_recall_failed", error=str(exc), wing=wing)
            return []
