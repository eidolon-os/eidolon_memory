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
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


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
        """Run a mutating statement, one at a time for this ledger."""

        if self._write_lock is None:  # pragma: no cover - constructor contract
            self._write_lock = asyncio.Lock()
        async with self._write_lock:
            return await asyncio.to_thread(fn, *args)

    @staticmethod
    async def _read(fn: Callable[..., T], *args: Any) -> T:
        """Run a read. Unserialised on purpose — see the module docstring."""

        return await asyncio.to_thread(fn, *args)
