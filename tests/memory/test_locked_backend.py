"""LockedBackend serialization contract (D1)."""

from __future__ import annotations

import asyncio

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
    await backend.delete("W1", rows[0].key)
    rows_after = await backend.get_all("")
    assert rows_after == []


@pytest.mark.asyncio
async def test_locked_backend_serializes_concurrent_writes() -> None:
    """All chromadb calls (including reads) share one ``asyncio.Lock``."""
    backend = LockedBackend(FakeMemoryBackend())

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
