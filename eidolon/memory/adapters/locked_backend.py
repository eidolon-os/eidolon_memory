"""asyncio.Lock-wrapped MemoryBackend (D1 single-writer + single-reader contract).

Every chromadb call — including reads — flows through one ``asyncio.Lock`` per
process. chromadb's read path is **not** strictly read-only at the SQLite layer
(WAL ``.shm`` updates, segment compaction, ``embeddings_queue`` writes), so
serializing read+read and read+write concurrency inside the agent_runner is
required to prevent the cache-divergence corruption class.

Lock latency is dominated by the awaited call; contention is negligible in the
chat-companion workload (writes are sparse per-turn, reads are single-flight
per LiveKit utterance).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from eidolon.memory.domain.errors import MemoryBackendUnsupported
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


class LockedBackend(MemoryBackend):
    """Single ``asyncio.Lock`` for all chromadb operations on one palace."""

    def __init__(self, inner: MemoryBackend, *, lock: asyncio.Lock | None = None) -> None:
        self._inner = inner
        self._lock = lock or asyncio.Lock()
        # A caller deadline may cancel its coroutine while ``asyncio.to_thread``
        # continues running.  Keep operation-owned tasks alive so the Realm lock
        # is released only when the actual backend operation has terminated.
        self._operations: set[asyncio.Task[Any]] = set()
        # Phase 2: optional in-memory working-memory ring. ``agent_runner``
        # assigns the actual instance after construction (so settings drive
        # ``maxlen`` without coupling LockedBackend to the config schema).
        self.working_memory: Any = None

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def inner(self) -> MemoryBackend:
        return self._inner

    @property
    def supports_scoped_search(self) -> bool:
        return bool(
            getattr(self._inner, "supports_scoped_search", False)
            and callable(getattr(self._inner, "search_scoped", None))
        )

    # ── capabilities of the inner store ──────────────────────────────────────
    #
    # These have to be declared here, not forwarded through ``__getattr__``.
    # Capabilities are discovered with ``isinstance`` against a runtime protocol,
    # and from Python 3.12 that check uses ``inspect.getattr_static``, which does
    # not run ``__getattr__`` — so a dynamically forwarded method exists when
    # called but is invisible to the check that decides whether to call it.
    #
    # A wrapper that simply omitted them would answer "no" to every capability,
    # which is worse than it sounds: warming is best-effort, so skipping it
    # raises nothing. It shipped that way for a round of work and surfaced only
    # as recall's graph lookup blowing its 300ms budget, because the embedding
    # model was still loading on the first request.
    #
    # The cost of declaring them is that ``isinstance`` now answers yes for any
    # wrapped store, including one whose inner store lacks the capability. Each
    # method therefore degrades to the same no-op the logic layer would have
    # chosen, so the answer stays truthful in effect if not in form.

    async def warm_read_path(self, *, wings) -> None:
        """Warm the inner store, if it can be warmed.

        Unserialised: this runs at startup before the process accepts traffic,
        so there is nothing to serialise against.
        """

        warm = getattr(self._inner, "warm_read_path", None)
        if warm is None:
            return
        await warm(wings=wings)

    async def room_graph(self):
        """Enumerate rooms under the lock, if the inner store can do it at all.

        Serialised, unlike warming: it reads Chroma's SQLite-backed cursor, which
        must not run alongside the write path.
        """

        read = getattr(self._inner, "room_graph", None)
        if read is None:
            return None
        return await self._serialized(read, name="room_graph")

    async def _serialized(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        name: str,
    ) -> T:
        """Run one backend operation under the Realm lock, cancellation-safely.

        ``asyncio.shield`` lets a deadline cancel the waiting caller without
        cancelling the task that owns the lock.  This matters for adapters that
        await ``asyncio.to_thread``: Python cannot stop that worker thread, so
        releasing the lock on caller cancellation would allow unsafe overlap.
        """

        started = asyncio.Event()

        async def _run() -> T:
            async with self._lock:
                started.set()
                return await operation()

        state = {"detached": False}
        task = asyncio.create_task(_run(), name=f"memory-backend:{name}")
        self._operations.add(task)

        def _completed(done: asyncio.Task[Any]) -> None:
            self._operations.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if state["detached"] and error is not None:
                log.warning(
                    "detached_memory_backend_operation_failed",
                    operation=name,
                    error=str(error),
                    error_type=type(error).__name__,
                )

        task.add_done_callback(_completed)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if started.is_set():
                state["detached"] = True
            else:
                # No storage work has begun.  Preserve normal cancellation
                # semantics instead of executing abandoned queued work later.
                task.cancel()
            raise

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
    ) -> list[MemoryWireRecord]:
        return await self._serialized(
            lambda: self._inner.search(query, wing=wing, n_results=n_results, room=room),
            name="search",
        )

    async def search_scoped(
        self,
        query: str,
        *,
        wings: list[str],
        n_results: int = 5,
        room: str | None = None,
        skip_closets: bool = False,
    ) -> list[MemoryWireRecord]:
        search_scoped = getattr(self._inner, "search_scoped", None)
        if not self.supports_scoped_search or search_scoped is None:
            raise MemoryBackendUnsupported("inner memory backend does not support scoped search")
        return await self._serialized(
            lambda: search_scoped(
                query,
                wings=wings,
                n_results=n_results,
                room=room,
                skip_closets=skip_closets,
            ),
            name="search_scoped",
        )

    async def ingest_text(
        self,
        *,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._serialized(
            lambda: self._inner.ingest_text(
                wing=wing,
                room=room,
                text=text,
                metadata=metadata,
            ),
            name="ingest_text",
        )

    async def ingest_fragment(self, fragment: MemoryFragment) -> None:
        await self._serialized(
            lambda: self._inner.ingest_fragment(fragment),
            name="ingest_fragment",
        )

    async def get(self, user_id: str, key: str) -> MemoryWireRecord | None:
        return await self._serialized(
            lambda: self._inner.get(user_id, key),
            name="get",
        )

    async def get_all(
        self,
        user_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MemoryWireRecord]:
        return await self._serialized(
            lambda: self._inner.get_all(user_id, limit=limit, offset=offset),
            name="get_all",
        )

    async def get_by_source_turn_id(
        self,
        memory_space_id: str,
        source_turn_id: str,
    ) -> MemoryWireRecord | None:
        return await self._serialized(
            lambda: self._inner.get_by_source_turn_id(memory_space_id, source_turn_id),
            name="get_by_source_turn_id",
        )

    async def delete(self, user_id: str, key: str) -> None:
        await self._serialized(
            lambda: self._inner.delete(user_id, key),
            name="delete",
        )

    async def delete_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        return await self._serialized(
            lambda: self._inner.delete_many(memory_space_id, keys),
            name="delete_many",
        )

    async def archive_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        return await self._serialized(
            lambda: self._inner.archive_many(memory_space_id, keys),
            name="archive_many",
        )
