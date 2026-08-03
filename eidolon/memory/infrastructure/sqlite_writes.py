"""Queue a ledger's writes in the event loop rather than inside SQLite.

Every ledger here runs its statements through ``asyncio.to_thread``, which keeps
the event loop free. Without anything else, concurrent writers to the same file
then meet inside SQLite and the loser waits out ``busy_timeout`` — up to five
seconds — **holding a thread pool worker the whole time**.

That was survivable while a process served one space, because the pool was the
space's own. Once one process serves several, the pool is shared: one space's
write contention occupies workers that every other space needs, and a busy owner
slows down an idle one. The contention itself does not grow — each space has its
own files — but its cost stops being local.

So writes queue here instead. A second writer waits on an ``asyncio.Lock``, which
costs nothing but a coroutine, and reaches SQLite only when the first has
finished. Same serialisation, paid in the right currency.

Reads deliberately do not queue. WAL allows one writer alongside many readers,
and taking the lock for reads would give that up for nothing.

The lock belongs to the ledger instance, and the router builds one ledger per
space — so this is per-space-per-ledger without anything having to arrange it.

Not needed by the PostgreSQL ledgers: a server handles concurrent writers itself,
and holding a lock across a network round trip is what the shared deployment
exists not to do.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def _ledger_concurrency_limit() -> int:
    """How many ledger statements may occupy thread pool workers at once.

    asyncio's default pool holds ``min(32, cores + 4)`` workers, and every ledger
    statement takes one for its duration. Six ledgers per space means three spaces
    can ask for more workers than exist — and a process serving one owner's three
    companions is the ordinary case, not a stress test. Past that point the vector
    store and the embedding session queue behind bookkeeping.

    So the ceiling is the core count, leaving the pool's spare workers for
    everything that is not a ledger. Bounding rather than serialising: several
    spaces still write at once, they just cannot take the whole pool.
    """

    return max(2, os.cpu_count() or 4)


_LEDGER_SLOTS: asyncio.Semaphore | None = None


def _slots() -> asyncio.Semaphore:
    """The process-wide bound, created on first use.

    Process-wide on purpose: the resource being protected is the one thread pool
    every space shares, so a per-space bound would not bound anything.
    """

    global _LEDGER_SLOTS
    if _LEDGER_SLOTS is None:
        _LEDGER_SLOTS = asyncio.Semaphore(_ledger_concurrency_limit())
    return _LEDGER_SLOTS


class SerialisedSqliteWrites:
    """Mixin giving a ledger one write lock, and the two ways to run a statement.

    Construct with :meth:`_init_write_lock` — ledgers have plain synchronous
    ``__init__`` methods that also open the file, and threading a mixin
    constructor through them buys nothing.
    """

    _write_lock: asyncio.Lock | None = None

    def _init_write_lock(self) -> None:
        self._write_lock = asyncio.Lock()

    async def _write(self, fn: Callable[..., T], *args: Any) -> T:
        """Run a mutating statement, one at a time for this ledger.

        Two bounds, doing different jobs: the lock serialises *this* ledger so its
        writers do not meet inside SQLite, and the semaphore caps how many ledger
        statements across *all* spaces hold thread pool workers at once.
        """

        if self._write_lock is None:  # pragma: no cover - constructor contract
            self._write_lock = asyncio.Lock()
        async with self._write_lock:
            async with _slots():
                return await asyncio.to_thread(fn, *args)

    @staticmethod
    async def _read(fn: Callable[..., T], *args: Any) -> T:
        """Run a read, unserialised but still bounded.

        WAL allows one writer alongside many readers, so reads are not queued
        against each other. They do occupy a worker while they run, though, so
        they pass through the same ceiling — a burst of reads across many spaces
        would otherwise starve the vector store just as writes would.
        """

        async with _slots():
            return await asyncio.to_thread(fn, *args)
