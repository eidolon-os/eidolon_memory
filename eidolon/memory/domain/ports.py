"""Abstract memory backend — implemented by MCP MemPalace or test fakes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from eidolon.memory.domain.wire import MemoryWireRecord


@runtime_checkable
class MemoryBackend(Protocol):
    """Async storage/search surface used by MemoryService (legacy RPC) and Worker."""

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
    ) -> list[MemoryWireRecord]:
        """Semantic search scoped to a wing (user / palace id)."""

    async def ingest_text(
        self,
        *,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append/index a verbatim text fragment (drawer semantics)."""

    async def get(self, user_id: str, key: str) -> MemoryWireRecord | None:
        """Exact id lookup when the backend supports stable doc ids."""

    async def get_all(self, user_id: str) -> list[MemoryWireRecord]:
        """List records for a wing/user."""

    async def delete(self, user_id: str, key: str) -> None:
        """Delete by logical user_id + key when supported."""
