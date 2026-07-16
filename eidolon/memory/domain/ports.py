"""Abstract memory backend — implemented by MemPalace or test fakes.

D1 lock contract:
    Serialization belongs to the backend adapter. Application services call
    read/write ports and never acquire the concrete storage lock themselves.
    The legacy ``lock`` attribute remains on ``MemoryReader`` for the working
    memory and palace-graph migration path; new storage capabilities must be
    expressed as narrow ports instead of bypassing the adapter.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from eidolon.memory.application.working_memory import WorkingMemoryRing
    from eidolon.memory.domain.command_status import CommandStatusRecord, CommandStatusStats
    from eidolon.memory.domain.dlq import DlqRecord, DlqReplayItem, DlqStats
    from eidolon.memory.domain.extraction_decision import ExtractionDecisionRecord
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
class ScopedMemoryReader(Protocol):
    """Optional optimized read capability for one query across many wings.

    Implementations own embedding reuse, storage details and serialization.
    Callers can fall back to ``MemoryReader.search`` fan-out when the capability
    is unavailable.
    """

    supports_scoped_search: bool

    async def search_scoped(
        self,
        query: str,
        *,
        wings: list[str],
        n_results: int = 5,
        room: str | None = None,
        skip_closets: bool = False,
    ) -> list[MemoryWireRecord]:
        """Search multiple wings while reusing backend-owned query work."""


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
class MemoryPrivacyAdmin(Protocol):
    """Tenant-scoped batch privacy mutations with verified success outcomes.

    The concrete backend wrapper must serialize each batch as one critical
    section. Candidate resolution intentionally stays outside this port and no
    cross-call transaction is implied.
    """

    async def delete_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        """Hard-delete a tenant-scoped batch and verify every key is invisible."""

    async def archive_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        """Mark a tenant-scoped batch do-not-recall and verify stored policy."""


@runtime_checkable
class MemoryBackend(MemoryReader, MemoryWriter, MemoryAdmin, MemoryPrivacyAdmin, Protocol):
    """Combined backend surface kept for compatibility with existing callers."""


@runtime_checkable
class ExtractionDecisionStore(Protocol):
    """Durable source for validated steward output, separate from projections."""

    async def get(
        self,
        memory_space_id: str,
        source_turn_id: str,
        extractor_version: str,
    ) -> ExtractionDecisionRecord | None: ...

    async def put_if_absent(
        self,
        record: ExtractionDecisionRecord,
    ) -> ExtractionDecisionRecord: ...


@runtime_checkable
class CommandStatusReader(Protocol):
    """Read-only projection used by MCP; never grants memory write access."""

    async def get(self, request_id: str) -> CommandStatusRecord | None:
        """Return the latest known outcome for one asynchronous command."""

    async def wait_terminal(
        self,
        request_id: str,
        *,
        timeout_seconds: float,
    ) -> CommandStatusRecord | None:
        """Wait on projection state without polling or locking memory storage."""

    async def stats(self) -> CommandStatusStats:
        """Return bounded-capacity and active-work metrics."""


@runtime_checkable
class CommandStatusWriter(Protocol):
    """Projection writer; cannot mutate memory facts or KG state."""

    async def record_accepted(self, request_id: str, *, kind: str) -> CommandStatusRecord: ...

    async def record_retrying(
        self,
        request_id: str,
        *,
        kind: str,
        error: str,
    ) -> CommandStatusRecord: ...

    async def record_applied(
        self,
        request_id: str,
        *,
        kind: str,
        resource_id: str | None = None,
    ) -> CommandStatusRecord: ...

    async def record_failed(
        self,
        request_id: str,
        *,
        kind: str,
        error: str,
    ) -> CommandStatusRecord: ...


@runtime_checkable
class CommandStatusStore(CommandStatusReader, CommandStatusWriter, Protocol):
    """Combined projection port used only at the composition boundary."""


@runtime_checkable
class DlqReader(Protocol):
    """Read/operations surface; it cannot mutate memory storage."""

    async def get(self, entry_id: str) -> DlqRecord | None: ...

    async def list(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DlqRecord]: ...

    async def stats(self) -> DlqStats: ...

    async def claim_replay(self, entry_id: str) -> DlqReplayItem | None: ...

    async def mark_replayed(self, entry_id: str) -> DlqRecord: ...

    async def release_replay(self, entry_id: str, *, error: str) -> DlqRecord: ...

    async def resolve(self, entry_id: str, *, note: str) -> DlqRecord: ...


@runtime_checkable
class DlqWriter(Protocol):
    async def add(
        self,
        *,
        subject: str,
        payload: bytes,
        error: str,
        deliveries: int,
    ) -> DlqRecord: ...


@runtime_checkable
class DlqStore(DlqReader, DlqWriter, Protocol):
    """Combined operational store used only at the composition boundary."""
