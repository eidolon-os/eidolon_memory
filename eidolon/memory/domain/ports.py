"""Abstract memory backend — implemented by MCP MemPalace or test fakes.

D1 lock contract:
    Backends that need to serialize concurrent access to underlying state
    (chromadb PersistentClient, SQLite cursor) expose an ``asyncio.Lock``
    as the ``lock`` attribute. Application-layer code that needs to share
    that lock across the read/write boundary(e.g. shared-embedding voice
    fast-path, palace_graph snapshot)reads ``backend.lock``; if ``None``,
    no locking is needed(unit-test fakes, in-memory implementations).

    This keeps the application layer decoupled from the concrete
    ``LockedBackend`` / ``LockedKnowledgeGraph`` classes — duck-typing
    against the Protocol, not the implementation.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from eidolon.memory.application.working_memory import WorkingMemoryRing
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.wire import MemoryWireRecord


@runtime_checkable
class MemoryReader(Protocol):
    """Async read surface used by recall clients and MCP read tools.

    Backends with single-owner state expose ``lock`` to share with the
    write path; otherwise ``lock`` is ``None`` (test fakes / pure in-mem).

    ``working_memory`` is an optional Phase 2 ring attached at runtime by
    agent_runner; recall code reads it via ``getattr(backend, "working_memory",
    None)`` so backends that don't carry one (test fakes) stay decoupled.
    """

    lock: asyncio.Lock | None
    working_memory: WorkingMemoryRing | None

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
    """Optional listing / by-key surface (``get`` / ``get_all`` / ``delete``);
    not every backend supports it. Used by MCP listing tools and replay paths,
    not by the hot recall path.
    """

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

        If ``user_id`` is blank (after stripping), adapters may enumerate the
        whole palace (paginated via ``limit`` / ``offset``) for operational listing.
        """

    async def get_by_source_turn_id(
        self,
        memory_space_id: str,
        source_turn_id: str,
    ) -> MemoryWireRecord | None:
        """Exact lookup by steward/source turn id for sync and benchmark probes."""

    async def delete(self, user_id: str, key: str) -> None:
        """Delete by logical user_id + key when supported."""


@runtime_checkable
class MemoryBackend(MemoryReader, MemoryWriter, MemoryAdmin, Protocol):
    """Combined backend surface kept for compatibility with existing callers."""
