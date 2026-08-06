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
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from eidolon_memory_contracts import MemoryIntent

    from eidolon.memory.application.working_memory import WorkingMemoryRing
    from eidolon.memory.domain.canonical_fact import (
        CanonicalFactHistoryRecord,
        CanonicalFactInvalidation,
        CanonicalFactRecord,
        CanonicalFactRegistration,
        CanonicalFactStats,
        ProjectionTarget,
    )
    from eidolon.memory.domain.command_status import CommandStatusRecord, CommandStatusStats
    from eidolon.memory.domain.commitment import (
        CommitmentApplyResult,
        CommitmentListPage,
        CommitmentRecord,
        CommitmentRevisionRecord,
    )
    from eidolon.memory.domain.dlq import DlqRecord, DlqReplayItem, DlqStats
    from eidolon.memory.domain.extraction_decision import ExtractionDecisionRecord
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.room_graph import RoomGraphSnapshot
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

    async def ingest_fragments(self, fragments: Sequence[MemoryFragment]) -> None:
        """Append/index one turn's fragments as a single write.

        On the port rather than left to callers looping, because the unit that
        matters is the turn and the cost is per *call*, not per fragment. Measured
        on a real palace, the six a turn may produce: six one-document upserts take
        32 ms, one six-document upsert takes 10.6 ms. The embedding is not where
        that goes — it is 1.2 ms of a 9 ms write — it is Chroma's per-call
        transaction, segment bookkeeping and index maintenance, paid six times.

        It also shortens the exclusive section by ~22 ms. A store that must hold a
        writer lock is one where the number of times you take it is a latency
        budget, and a recall arriving mid-turn waits for whichever write holds it.

        Failure is all-or-nothing, which is what the caller already assumed: the
        turn processor wraps the whole loop in one try/except that NAKs the turn,
        and drawer ids are deterministic, so a replay re-writes the same rows.
        """


@runtime_checkable
class MemoryAdmin(Protocol):
    """Optional listing / by-key surface (``get`` / ``get_all`` / ``delete``);
    not every backend supports it. Used by MCP listing tools and replay paths,
    not by the hot recall path.
    """

    async def get(self, user_id: str, key: str) -> MemoryWireRecord | None:
        """Exact id lookup when the backend supports stable doc ids."""

    async def get_many(self, user_id: str, keys: list[str]) -> list[MemoryWireRecord]:
        """The same lookup for a batch, in one round trip.

        Here because a loop over ``get`` is not equivalent on this hot-ish path:
        every call crosses the space lock, and the forget path reads up to a
        hundred drawers before it may delete any of them. Missing ids are
        omitted, so the result is not positionally aligned with ``keys``.
        """

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


# What a vector store has to provide. Named separately from MemoryBackend
# because that name says where an implementation sits, not what it does — and
# what it does is the thing a replacement has to match.
#
# Recall's hot path reads only RECALL_HOT_PATH_FIELDS off a returned record.
# Everything else in the record is either operational or specific to how one
# backend stores things, and a backend that populated only these fields would
# still serve conversation correctly.
VectorStorePort = MemoryBackend

RECALL_HOT_PATH_FIELDS = frozenset({"text", "wing", "room", "source_file", "similarity"})
"""The fields a recall must not need more than.

Enforced by tests/memory/test_backend_contract.py. The point is to keep the
contract small enough that a different vector store is a plausible substitution —
if recall starts depending on a sixth field, that has to be a deliberate widening
of the contract rather than something a single call site quietly introduces.
"""


@runtime_checkable
class WarmableBackend(Protocol):
    """A store whose first read is much slower than the rest.

    Embedded storage pays for the first request of a process: an ONNX model is
    loaded, the vector index is read off disk, collection handles are opened. A
    remote store has already paid all of it on the server, so warming it is
    pointless rather than merely cheap.

    Declared as a capability the store offers, not a fact the caller looks up.
    The alternative — asking which backend is configured and skipping warmup for
    the remote ones — puts a list of backend names in the startup path, so every
    new store means editing code that has nothing to do with storage, and the
    check silently does the wrong thing for a store nobody thought to add.
    """

    async def warm_read_path(self, *, wings: Sequence[str]) -> None:
        """Do the first-request work now, on the wings most likely to be read.

        Best-effort by contract: a failure here means the first real request is
        slow, not that the service is broken, so callers log and carry on.
        """
        ...


@runtime_checkable
class RoomGraphBackend(Protocol):
    """A store that can enumerate its rooms and which wings they appear under.

    Separate from the main port because it is an inspection feature, not part of
    serving conversation: a store that cannot do it should still be a usable
    memory backend. Callers check for the capability and report the graph as
    unavailable rather than treating its absence as a failure.
    """

    async def room_graph(self) -> RoomGraphSnapshot | None:
        """Every room this store knows about, or None if it has no palace yet.

        Returns everything and leaves ranking and capping to the caller —
        deciding what is worth showing is a presentation question, and a store
        that answered it would make that decision unchangeable from outside.
        """
        ...


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
class CanonicalFactReader(Protocol):
    """Read-only operational view; never participates in recall."""

    async def stats(self) -> CanonicalFactStats: ...

    async def history(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
        *,
        object_value: str | None = None,
        limit: int = 100,
    ) -> list[CanonicalFactHistoryRecord]: ...


@runtime_checkable
class CanonicalFactWriter(Protocol):
    """Exact structured fact identity and evidence write port."""

    async def register(
        self,
        intent: MemoryIntent,
        *,
        targets: set[ProjectionTarget],
    ) -> CanonicalFactRegistration: ...

    async def active_for_slot(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
    ) -> list[CanonicalFactRecord]: ...

    async def get_fact(
        self,
        memory_space_id: str,
        subject: str,
        predicate: str,
        object_value: str,
    ) -> CanonicalFactRecord | None: ...

    async def register_reactivation(
        self,
        intent: MemoryIntent,
        *,
        targets: set[ProjectionTarget],
    ) -> CanonicalFactRegistration: ...

    async def mark_reactivated(
        self,
        memory_space_id: str,
        intent_id: str,
    ) -> None: ...

    async def mark_projected(
        self,
        memory_space_id: str,
        assertion_id: str,
        *,
        targets: set[ProjectionTarget],
    ) -> None: ...

    async def mark_projection_pending(
        self,
        memory_space_id: str,
        assertion_id: str,
        *,
        targets: set[ProjectionTarget],
    ) -> None: ...

    async def register_invalidation(
        self,
        intent: MemoryIntent,
    ) -> CanonicalFactInvalidation: ...

    async def mark_invalidated(
        self,
        memory_space_id: str,
        intent_id: str,
    ) -> None: ...


@runtime_checkable
class CanonicalFactStore(CanonicalFactReader, CanonicalFactWriter, Protocol):
    """Combined canonical ledger port used only at the composition boundary."""


@runtime_checkable
class CommitmentReader(Protocol):
    async def get(
        self, memory_space_id: str, commitment_id: str
    ) -> CommitmentRecord | None: ...

    async def list_current(
        self,
        memory_space_id: str,
        *,
        include_terminal: bool = False,
        limit: int = 100,
    ) -> list[CommitmentRecord]: ...

    async def list_current_page(
        self,
        memory_space_id: str,
        *,
        include_terminal: bool = False,
        limit: int = 100,
    ) -> CommitmentListPage: ...

    async def history(
        self,
        memory_space_id: str,
        commitment_id: str,
        *,
        limit: int = 200,
    ) -> list[CommitmentRevisionRecord]: ...


@runtime_checkable
class CommitmentWriter(Protocol):
    async def apply(self, intent: MemoryIntent) -> CommitmentApplyResult: ...

    async def mark_projected(
        self,
        memory_space_id: str,
        commitment_id: str,
        revision: int,
        *,
        targets: set[ProjectionTarget],
    ) -> None: ...


@runtime_checkable
class CommitmentStore(CommitmentReader, CommitmentWriter, Protocol):
    """Combined commitment aggregate at the Realm composition boundary."""


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


@runtime_checkable
class SyncLedgerPort(Protocol):
    """Idempotency record for device sync batches replayed after being offline.

    Not split into reader and writer like the other ledgers: both methods exist
    to serve one decision at one call site — has this batch already been applied
    — and a reader without its writer could not answer it correctly.

    Async like every other ledger. It was synchronous while the only
    implementation was a local file, where blocking is a few microseconds; over a
    network it would stall the event loop for a round trip on every event in a
    batch, so the signature had to be the one both storages can honour.
    """

    async def seen(self, *, event_id: str, idempotency_hash: str) -> bool:
        """Whether this event or an identical payload was already applied."""
        ...

    async def mark_synced(
        self,
        *,
        event_id: str,
        device_id: str,
        instance_id: str,
        turn_id: str,
        idempotency_hash: str,
    ) -> None:
        """Record that this event was applied. Idempotent."""
        ...
