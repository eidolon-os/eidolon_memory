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
from typing import Any

from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord


class LockedBackend(MemoryBackend):
    """Single ``asyncio.Lock`` for all chromadb operations on one palace."""

    def __init__(self, inner: MemoryBackend, *, lock: asyncio.Lock | None = None) -> None:
        self._inner = inner
        self._lock = lock or asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def inner(self) -> MemoryBackend:
        return self._inner

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
    ) -> list[MemoryWireRecord]:
        async with self._lock:
            return await self._inner.search(query, wing=wing, n_results=n_results, room=room)

    async def ingest_text(
        self,
        *,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            await self._inner.ingest_text(wing=wing, room=room, text=text, metadata=metadata)

    async def ingest_fragment(self, fragment: MemoryFragment) -> None:
        async with self._lock:
            await self._inner.ingest_fragment(fragment)

    async def get(self, user_id: str, key: str) -> MemoryWireRecord | None:
        async with self._lock:
            return await self._inner.get(user_id, key)

    async def get_all(
        self,
        user_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MemoryWireRecord]:
        async with self._lock:
            return await self._inner.get_all(user_id, limit=limit, offset=offset)

    async def delete(self, user_id: str, key: str) -> None:
        async with self._lock:
            await self._inner.delete(user_id, key)
