"""Ephemeral request/reply queries between memory helper processes.

These subjects are intentionally **not** part of the durable JetStream memory
stream. They are live control-plane reads: if the owning agent runner is not
up, callers should retry later rather than replay stale query requests.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import nats
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError
from eidolon_sdk.memory.subjects import validate_memory_space_id

from eidolon.memory.config.memory_settings import MemorySettings

MEMORY_QUERY_BASE = "eidolon.memory.query"


def memory_list_drawers_query_subject(memory_space_id: str) -> str:
    """Return the per-memory-space NATS request subject for drawer snapshots."""

    return f"{MEMORY_QUERY_BASE}.{validate_memory_space_id(memory_space_id)}.list_drawers"


class MemoryQueryError(RuntimeError):
    """Raised when the agent runner query responder returns an error."""


class NatsMemoryQueryClient:
    """Small NATS core request/reply client for read-only memory snapshots."""

    def __init__(self, *, nats_url: str, timeout_seconds: float = 5.0) -> None:
        self._url = nats_url
        self._timeout = timeout_seconds
        self._nc: nats.NATS | None = None

    @classmethod
    def from_memory_settings(
        cls, settings: MemorySettings, *, timeout_seconds: float = 5.0
    ) -> "NatsMemoryQueryClient":
        return cls(nats_url=settings.nats.url, timeout_seconds=timeout_seconds)

    async def connect(self) -> None:
        if self._nc is not None:
            return
        self._nc = await nats.connect(self._url)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
        self._nc = None

    async def list_drawers(
        self,
        *,
        memory_space_id: str,
        limit: int,
        offset: int = 0,
        include_private: bool = False,
    ) -> dict[str, Any]:
        if self._nc is None:
            await self.connect()
        assert self._nc is not None
        subject = memory_list_drawers_query_subject(memory_space_id)
        payload = {
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
            "include_private": bool(include_private),
        }
        msg = await self._nc.request(
            subject,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self._timeout,
        )
        data = json.loads(msg.data.decode("utf-8") or "{}")
        if isinstance(data, dict) and data.get("error"):
            err = str(data.get("error") or "memory query failed")
            err_type = str(data.get("error_type") or "MemoryQueryError")
            raise MemoryQueryError(f"{err_type}: {err}")
        if not isinstance(data, dict):
            raise MemoryQueryError(f"unexpected memory query response: {type(data).__name__}")
        return data

    async def wait_until_ready(
        self,
        *,
        memory_space_id: str,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        """Wait until the owning agent runner is answering query requests."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        last_error: Exception | None = None
        while True:
            try:
                await self.list_drawers(
                    memory_space_id=memory_space_id,
                    limit=1,
                    offset=0,
                    include_private=False,
                )
                return
            except (NoRespondersError, NatsTimeoutError) as exc:
                last_error = exc
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise last_error
                await asyncio.sleep(min(max(0.05, poll_interval_seconds), remaining))
