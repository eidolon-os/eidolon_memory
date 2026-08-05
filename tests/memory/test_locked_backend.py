"""LockedBackend serialization contract (D1)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.domain.fragments import MemoryFragment


@pytest.mark.asyncio
async def test_locked_backend_passes_through_all_methods() -> None:
    backend = LockedBackend(FakeMemoryBackend())
    await backend.ingest_text(wing="W1", room="R1", text="hello", metadata={"k": "v"})
    rows = await backend.get_all("")
    assert len(rows) == 1
    assert rows[0].value == "hello"
    rec = await backend.get("W1", rows[0].key)
    assert rec is not None
    hits = await backend.search("hello", wing="W1", n_results=5)
    assert hits
    archived = await backend.archive_many("W1", [rows[0].key])
    assert archived == [rows[0].key]
    rec = await backend.get("W1", rows[0].key)
    assert rec is not None
    assert rec.metadata["privacy"] == "do_not_recall"
    await backend.delete_many("W1", [rows[0].key])
    rows_after = await backend.get_all("")
    assert rows_after == []


@pytest.mark.asyncio
async def test_locked_backend_serializes_concurrent_writes() -> None:
    """A write excludes readers: they share one space lock, on opposite sides."""
    holding = asyncio.Event()
    release = asyncio.Event()

    class SlowFake(FakeMemoryBackend):
        async def ingest_text(self, **kwargs) -> None:
            holding.set()
            await release.wait()
            await super().ingest_text(**kwargs)

    slow = LockedBackend(SlowFake())

    async def writer() -> None:
        await slow.ingest_text(wing="W1", room="R1", text="a", metadata={})

    async def reader() -> int:
        # waiting for the lock implies it's actually held
        return len(await slow.get_all(""))

    write_task = asyncio.create_task(writer())
    await holding.wait()
    # the reader must block until the writer releases the lock
    read_task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)
    assert not read_task.done(), "reader ran while writer held lock — lock is broken"
    release.set()
    await write_task
    assert await read_task == 1


@pytest.mark.asyncio
async def test_timeout_does_not_release_lock_before_worker_thread_finishes() -> None:
    """Cancelling a caller must not let a *conflicting* operation overlap.

    ``asyncio.to_thread`` cannot stop its worker thread when the awaiting task is
    cancelled. The space lock therefore belongs to the backend operation, not to
    the caller's lifetime.

    The pair here is a read whose caller times out and a **write** that follows.
    It used to be read-then-read, which stopped expressing the invariant once the
    lock learned to admit concurrent readers: two reads overlapping is now correct,
    so that version would have failed for the right reason and told us nothing
    about cancellation. A write is what must still wait.
    """
    worker_started = threading.Event()
    release_worker = threading.Event()
    write_entered = asyncio.Event()

    class ThreadedFake(FakeMemoryBackend):
        async def search(self, *args, **kwargs):
            def _blocking_search():
                worker_started.set()
                release_worker.wait(timeout=2.0)
                return []

            return await asyncio.to_thread(_blocking_search)

        async def ingest_text(self, **kwargs):
            write_entered.set()
            return await super().ingest_text(**kwargs)

    backend = LockedBackend(ThreadedFake())
    first = asyncio.create_task(backend.search("slow", wing="W1"))
    assert await asyncio.to_thread(worker_started.wait, 1.0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(first, timeout=0.02)

    second = asyncio.create_task(
        backend.ingest_text(wing="W1", room="R1", text="after", metadata=None)
    )
    await asyncio.sleep(0.05)
    assert not write_entered.is_set(), (
        "caller timeout released the space lock while its reader thread was still active"
    )

    release_worker.set()
    await second


@pytest.mark.asyncio
async def test_reads_overlap_and_a_write_still_excludes_them() -> None:
    """The point of the readers-writer lock, both halves.

    Two reads overlapping is what makes the graph lookup in
    ``recall_with_kg_fusion`` able to run alongside the vector search it is started
    with rather than after it — under the mutex this replaced, that lookup spent its
    50ms voice budget waiting for the lock and was then reported as a graph timeout.

    A write excluding readers is the half that must not regress, because chroma
    needs SQLite ``EXCLUSIVE`` for a write and the palace's journal is a rollback
    journal, so an overlapping reader would block for ``busy_timeout`` or fail.
    """
    inside_reads = 0
    peak_concurrent_reads = 0
    reads_during_write = 0
    writing = False

    class CountingFake(FakeMemoryBackend):
        async def search(self, *args, **kwargs):
            nonlocal inside_reads, peak_concurrent_reads, reads_during_write
            inside_reads += 1
            peak_concurrent_reads = max(peak_concurrent_reads, inside_reads)
            if writing:
                reads_during_write += 1
            try:
                await asyncio.sleep(0.02)
                return []
            finally:
                inside_reads -= 1

        async def ingest_text(self, **kwargs):
            nonlocal writing
            writing = True
            try:
                await asyncio.sleep(0.02)
            finally:
                writing = False

    backend = LockedBackend(CountingFake())
    await asyncio.gather(*[backend.search(f"q{i}", wing="W1") for i in range(6)])
    assert peak_concurrent_reads == 6, (
        f"reads did not overlap: peak was {peak_concurrent_reads} of 6"
    )

    await asyncio.gather(
        backend.ingest_text(wing="W1", room="R1", text="t", metadata=None),
        *[backend.search(f"during{i}", wing="W1") for i in range(4)],
    )
    assert reads_during_write == 0, (
        f"{reads_during_write} read(s) ran while a write held the lock"
    )


@pytest.mark.asyncio
async def test_cancelled_queued_operation_does_not_execute_later() -> None:
    """Shield only operations that started; abandoned queued work stays cancelled."""
    holding = asyncio.Event()
    release = asyncio.Event()

    class HoldingFake(FakeMemoryBackend):
        async def get_all(self, *args, **kwargs):
            holding.set()
            await release.wait()
            return await super().get_all(*args, **kwargs)

    inner = HoldingFake()
    backend = LockedBackend(inner)
    first = asyncio.create_task(backend.get_all("W1"))
    await holding.wait()

    queued = asyncio.create_task(
        backend.ingest_text(wing="W1", room="R1", text="must-not-run", metadata={})
    )
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    release.set()
    assert await first == []
    await asyncio.sleep(0)
    assert inner.ingests == []


@pytest.mark.asyncio
async def test_locked_backend_ingest_fragment_route() -> None:
    backend = LockedBackend(FakeMemoryBackend())
    fragment = MemoryFragment(
        memory_id="frag1",
        memory_space_id="r:alice:default",
        memory_realm_id="r:alice:default",
        owner_id="alice",
        companion_id="test",
        wing="Wing_Profile",
        room="profile_core",
        content="hello",
        memory_type="profile",
        importance=4,
        confidence=0.9,
        source_turn_id="turn1",
        session_id="sess",
    )
    await backend.ingest_fragment(fragment)
    rows = await backend.get_all("")
    assert len(rows) == 1
    assert rows[0].value == "hello"
