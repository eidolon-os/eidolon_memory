"""Single write/ingest entry — both public write paths call into here (D1)."""

from __future__ import annotations

import asyncio
from typing import Any

from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import MemoryBackend


async def ingest_fragment(
    backend: MemoryBackend,
    *,
    wing: str,
    room: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    serialize_lock: asyncio.Lock | None = None,
) -> None:
    """Persist one verbatim fragment via ``MemoryBackend.ingest_text``.

    D1: the only write path is the agent_runner's in-process NATS subscriber
    → Steward → ``ingest_fragment`` → ``LockedBackend.ingest_text``. The
    optional ``serialize_lock`` is kept for tests / scripts that wrap a raw
    backend; production wraps the backend in :class:`LockedBackend`, so this
    parameter is normally ``None``.
    """
    meta = metadata if metadata is not None else {}
    if serialize_lock is None:
        await backend.ingest_text(wing=wing, room=room, text=text, metadata=meta)
        return
    async with serialize_lock:
        await backend.ingest_text(wing=wing, room=room, text=text, metadata=meta)


async def ingest_memory_fragment(
    backend: MemoryBackend,
    fragment: MemoryFragment,
    *,
    serialize_lock: asyncio.Lock | None = None,
) -> None:
    """Persist one structured steward fragment via the backend."""
    if serialize_lock is None:
        await backend.ingest_fragment(fragment)
        return
    async with serialize_lock:
        await backend.ingest_fragment(fragment)
