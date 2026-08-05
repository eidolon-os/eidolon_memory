"""The lock every space's storage passes through.

Six properties, and each of them is something the exclusive mutex this replaced
either did not have or did not need. They are asserted on the primitive rather than
through a backend because a lock is the one thing where "it seemed to work" and "it
is correct" diverge only under load, in production, once.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from eidolon.memory.domain.space_lock import SpaceLock


async def test_readers_run_at_the_same_time() -> None:
    """The whole reason for the change.

    Chroma's own HNSW segment admits concurrent readers and the graph's SQLite is in
    WAL so a reader is not blocked by the current write. A mutex on top threw both
    away.
    """

    lock = SpaceLock()
    inside = 0
    peak = 0

    async def reader() -> None:
        nonlocal inside, peak
        async with lock.reader():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    started = time.perf_counter()
    await asyncio.gather(*[reader() for _ in range(8)])
    elapsed = time.perf_counter() - started

    assert peak == 8, f"readers serialised: peak was {peak}"
    # Eight 20ms readers in ~20ms rather than ~160ms.
    assert elapsed < 0.10, f"eight concurrent readers took {elapsed * 1000:.0f}ms"


async def test_writers_exclude_each_other() -> None:
    lock = SpaceLock()
    inside = 0
    peak = 0

    async def writer() -> None:
        nonlocal inside, peak
        async with lock.writer():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(*[writer() for _ in range(5)])

    assert peak == 1, f"{peak} writers were inside at once"


async def test_a_writer_excludes_readers() -> None:
    """A turn writes to the vector store and the graph, and chroma needs SQLite
    ``EXCLUSIVE`` for the first of those. An overlapping reader would block for
    ``busy_timeout`` or fail outright."""

    lock = SpaceLock()
    log: list[str] = []

    async def writer() -> None:
        async with lock.writer():
            log.append("w-in")
            await asyncio.sleep(0.03)
            log.append("w-out")

    async def reader(i: int) -> None:
        await asyncio.sleep(0.01)  # arrive while the writer holds it
        async with lock.reader():
            log.append(f"r{i}")

    await asyncio.gather(writer(), reader(0), reader(1))

    assert log[:2] == ["w-in", "w-out"], log


async def test_a_waiting_writer_holds_off_new_readers() -> None:
    """Writer-preferring, deliberately.

    Readers are recalls and writers are turns. A reader-preferring lock lets a
    steady stream of recalls starve a write indefinitely, and a turn that never
    lands is a memory the user told us and we lost. Chroma's own ``ReadWriteLock``
    is reader-preferring, which is right for the in-memory index it guards and
    wrong here.
    """

    lock = SpaceLock()
    log: list[str] = []

    async def long_reader() -> None:
        async with lock.reader():
            log.append("r0-in")
            await asyncio.sleep(0.05)
            log.append("r0-out")

    async def writer() -> None:
        await asyncio.sleep(0.01)  # queues behind r0
        async with lock.writer():
            log.append("W")

    async def late_reader() -> None:
        await asyncio.sleep(0.02)  # arrives after the writer is already waiting
        async with lock.reader():
            log.append("r1")

    await asyncio.gather(long_reader(), writer(), late_reader())

    assert log.index("W") < log.index("r1"), log


async def test_entering_it_bare_takes_the_writer_side() -> None:
    """So a call site nobody has revisited keeps the semantics it had under the
    mutex. Forgetting to narrow a read to ``reader()`` then costs concurrency
    rather than correctness — the one direction that is safe to get wrong."""

    lock = SpaceLock()
    inside = 0
    peak = 0

    async def legacy() -> None:
        nonlocal inside, peak
        async with lock:
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(*[legacy() for _ in range(3)])

    assert peak == 1


async def test_cancelling_a_waiting_writer_does_not_wedge_readers() -> None:
    """A waiting writer holds readers off. If it is cancelled while waiting — a
    caller deadline — nothing else is left to tell them they may proceed."""

    lock = SpaceLock()

    async def hold_read() -> None:
        async with lock.reader():
            await asyncio.sleep(0.05)

    holder = asyncio.create_task(hold_read())
    await asyncio.sleep(0.01)
    waiting_writer = asyncio.create_task(lock.acquire_write())
    await asyncio.sleep(0.01)
    waiting_writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_writer

    # Readers must still be admitted, and promptly.
    await asyncio.wait_for(asyncio.gather(hold_read(), hold_read()), timeout=1.0)
    await holder

    assert lock.readers == 0
    assert lock.writing is False


async def test_it_does_not_recurse() -> None:
    """Same as the ``asyncio.Lock`` it replaced. Nothing in the codebase nests —
    the ring is only touched outside a held lock, and a turn's vector write and
    graph write are sequential acquisitions — so this is a property to rely on
    rather than defend against, and worth pinning so it stays true on purpose."""

    lock = SpaceLock()
    async with lock.writer():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(lock.acquire_write(), timeout=0.05)
