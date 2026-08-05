"""One space's readers-writer lock.

Everything that touches a space's storage is serialised through here: the vector
store, the graph, and the working-memory ring. One lock per space, because a turn
writes to the vector store and the graph atomically and one critical section over
the pair beats an ordering between two.

**Why not a plain mutex, which is what this replaces.** Both layers underneath
already distinguish readers from writers, and a single mutex threw that away:

* Chroma's persistent HNSW segment holds a ``ReadWriteLock`` — its query path
  takes a read lock and ``_write_records`` takes a write lock, so many concurrent
  readers are what the library is built for. Its SQLite side hands out one
  connection per thread (``PerThreadPool``), so concurrent readers there take
  SQLite ``SHARED`` locks and do not block each other either.
* Our own graph opens its SQLite in WAL mode, with the comment "WAL so a reader is
  never blocked by the turn currently writing" — and then every read went through
  the mutex, which is precisely what WAL was enabled to avoid.

The concrete cost was in the recall path. ``recall_with_kg_fusion`` starts the
vector search and the graph lookup as two concurrent tasks and gives the graph a
50 ms budget on the voice path. Sharing one mutex, the graph task spent that budget
waiting for the vector search to release the lock, then timed out — reported as a
graph timeout, which reads as "the graph is slow" rather than "the graph never
ran". Two ports of call for the same conclusion: the lock was stricter than
anything underneath it required.

**Writer-preferring**, and that direction is deliberate. Readers are recalls, on a
300 ms voice deadline; writers are turns, and a turn that never lands is a memory
the user told us and we lost. A reader-preferring lock lets a steady stream of
recalls starve a write indefinitely. Chroma's own ``ReadWriteLock`` is
reader-preferring for exactly this reason inverted — it protects an in-memory index
where writes are cheap — which is not our situation.

**Not reentrant**, like the ``asyncio.Lock`` it replaces. Acquiring it twice in one
task deadlocks. That is safe to rely on rather than defend against, because the
mutex had the same property and nothing in the codebase nests: the ring is only
ever touched outside a held lock, and a turn's vector write and graph write are
sequential acquisitions, not nested ones.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType


class SpaceLock:
    """Many concurrent readers, or one exclusive writer.

    Entering it directly (``async with lock:``) takes the **writer** side. That
    default is chosen so a call site nobody has looked at yet keeps exactly the
    semantics it had under the mutex — narrowing a read to ``reader()`` is then an
    opt-in change someone made on purpose, and forgetting to do it costs
    concurrency rather than correctness.
    """

    __slots__ = ("_condition", "_readers", "_waiting_writers", "_writing")

    def __init__(self) -> None:
        self._readers = 0
        self._writing = False
        self._waiting_writers = 0
        # Built on first use, not here. A space's handles are constructed inside
        # ``asyncio.to_thread`` — no running loop — and asyncio primitives bind to
        # the loop that first awaits them. Constructing lazily makes it impossible
        # for this to bind to the throwaway loop a process resolves its first
        # space in rather than to the one that serves traffic.
        self._condition: asyncio.Condition | None = None

    def _cond(self) -> asyncio.Condition:
        # No await between the check and the assignment, so two coroutines in the
        # same loop cannot both create one.
        if self._condition is None:
            self._condition = asyncio.Condition()
        return self._condition

    @property
    def readers(self) -> int:
        """Readers currently inside. For tests and diagnostics."""

        return self._readers

    @property
    def writing(self) -> bool:
        """Whether a writer is currently inside. For tests and diagnostics."""

        return self._writing

    async def acquire_read(self) -> None:
        cond = self._cond()
        async with cond:
            await cond.wait_for(lambda: not self._writing and self._waiting_writers == 0)
            self._readers += 1

    async def release_read(self) -> None:
        cond = self._cond()
        async with cond:
            self._readers -= 1
            if self._readers == 0:
                # Only a writer can be waiting on this, and only once the last
                # reader leaves — notifying earlier would wake it to find readers
                # still inside.
                cond.notify_all()

    async def acquire_write(self) -> None:
        cond = self._cond()
        async with cond:
            # Counted before waiting, so readers arriving from now on queue behind
            # this writer instead of extending the run it is waiting out.
            self._waiting_writers += 1
            try:
                await cond.wait_for(lambda: not self._writing and self._readers == 0)
            finally:
                self._waiting_writers -= 1
                if self._waiting_writers == 0 and not self._writing:
                    # Cancelled while waiting, and the last writer that wanted in.
                    # Readers held off by ``_waiting_writers`` are free now and
                    # nothing else is going to tell them.
                    cond.notify_all()
            self._writing = True

    async def release_write(self) -> None:
        cond = self._cond()
        async with cond:
            self._writing = False
            cond.notify_all()

    @asynccontextmanager
    async def reader(self) -> AsyncIterator[None]:
        """Admit a reader, unless a writer holds or is waiting for the lock."""

        await self.acquire_read()
        try:
            yield
        finally:
            await self.release_read()

    @asynccontextmanager
    async def writer(self) -> AsyncIterator[None]:
        """Admit one writer, once every reader already inside has left."""

        await self.acquire_write()
        try:
            yield
        finally:
            await self.release_write()

    # ── the mutex's own interface, kept ──────────────────────────────────────
    #
    # No per-entry state on the instance: several tasks enter concurrently, so
    # anything stored here would be clobbered by whichever entered last.

    async def __aenter__(self) -> None:
        await self.acquire_write()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release_write()
