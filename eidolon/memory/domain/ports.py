"""Abstract memory backend — implemented by MCP MemPalace or test fakes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.wire import MemoryWireRecord


@runtime_checkable
class MemoryReader(Protocol):
    """Async read surface used by recall clients and MCP read tools."""

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
    ) -> list[MemoryWireRecord]:
        """Semantic search scoped to a wing (user / palace id)."""


@runtime_checkable
class MemoryWriter(Protocol):
    """Async write surface used by steward pipelines."""

    async def ingest_text(
        self,
        *,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append/index a verbatim text fragment (drawer semantics)."""

    async def ingest_fragment(self, fragment: MemoryFragment) -> None:
        """Append/index a structured steward fragment."""


@runtime_checkable
class MemoryAdmin(Protocol):
    """Optional administrative surface; not every backend supports it."""

    async def get(self, user_id: str, key: str) -> MemoryWireRecord | None:
        """Exact id lookup when the backend supports stable doc ids."""

    async def get_all(
        self,
        user_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MemoryWireRecord]:
        """Tenant filter: matches metadata ``user_id`` or ``wing``.

        If ``user_id`` is blank (after stripping), adapters may enumerate the whole palace
        (paginated via ``limit`` / ``offset``) for administrative listing.
        """

    async def delete(self, user_id: str, key: str) -> None:
        """Delete by logical user_id + key when supported."""


@runtime_checkable
class MemoryBackend(MemoryReader, MemoryWriter, MemoryAdmin, Protocol):
    """Combined backend surface kept for compatibility with existing callers."""
