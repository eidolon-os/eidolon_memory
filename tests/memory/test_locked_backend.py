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
    """All chromadb calls (including reads) share one ``asyncio.Lock``."""
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
    """Cancelling a caller must not let a still-running sync backend overlap.

    ``asyncio.to_thread`` cannot stop its worker thread when the awaiting task
    is cancelled.  The Realm lock therefore belongs to the backend operation,
    not to the caller's lifetime.
    """
    worker_started = threading.Event()
    release_worker = threading.Event()
    second_operation_entered = asyncio.Event()

    class ThreadedFake(FakeMemoryBackend):
        async def search(self, *args, **kwargs):
            def _blocking_search():
                worker_started.set()
                release_worker.wait(timeout=2.0)
                return []

            return await asyncio.to_thread(_blocking_search)

        async def get_all(self, *args, **kwargs):
            second_operation_entered.set()
            return await super().get_all(*args, **kwargs)

    backend = LockedBackend(ThreadedFake())
    first = asyncio.create_task(backend.search("slow", wing="W1"))
    assert await asyncio.to_thread(worker_started.wait, 1.0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(first, timeout=0.02)

    second = asyncio.create_task(backend.get_all("W1"))
    await asyncio.sleep(0.05)
    assert not second_operation_entered.is_set(), (
        "caller timeout released the Realm lock while its worker thread was still active"
    )

    release_worker.set()
    assert await second == []


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
