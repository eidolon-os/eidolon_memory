"""Single write/ingest entry — both public write paths call into here."""

from __future__ import annotations

import asyncio
from typing import Any

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

    **两条写入路径**在落到 MemPalace 前都经此函数，保证底层 MCP 调用一致：

    1. **同步 MCP 路径**：Agent → ``MemoryClient.store`` → NATS ``MEMORY_STORE`` →
       :class:`eidolon.memory.application.memory_service.MemoryService`（可带
       ``serialize_lock``，避免并发写乱序）。
    2. **异步路径**：``JetStreamTurnPublisher`` → Worker → Steward → 同一
       ``ingest_fragment``（通常 ``serialize_lock=None``）。

    具体 ``call_tool`` 仍由 :class:`eidolon.memory.adapters.McpMemPalaceBackend`
    实现；本函数只做「加锁（可选）+ 调 ``ingest_text``」这一层归一。
    """
    meta = metadata if metadata is not None else {}
    if serialize_lock is None:
        await backend.ingest_text(wing=wing, room=room, text=text, metadata=meta)
        return
    async with serialize_lock:
        await backend.ingest_text(wing=wing, room=room, text=text, metadata=meta)
