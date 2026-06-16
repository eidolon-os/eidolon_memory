"""Publish ``MemoryCommandPayload`` (admin / agent KG edits) to JetStream.

All writes — chat turns, KG triples, future deletes — flow through the same
JetStream stream so D5 rebuild-from-replay is the single source of truth.
This module is the publisher half; the worker side is
``application.turn_processor.process_command_message``.
"""

from __future__ import annotations

import json
from typing import Any

import nats
from eidolon_sdk.memory import MemoryCommandPayload, memory_command_subject

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class JetStreamCommandPublisher:
    """Connects to NATS and publishes ``MemoryCommandPayload`` per user_id subject."""

    def __init__(self, *, nats_url: str, stream_name: str) -> None:
        self._url = nats_url
        self._stream = stream_name
        self._nc: nats.NATS | None = None
        self._js: Any = None

    @classmethod
    def from_memory_settings(cls, settings: MemorySettings) -> "JetStreamCommandPublisher":
        return cls(nats_url=settings.nats.url, stream_name=settings.nats.stream)

    async def connect(self) -> None:
        if self._nc is not None:
            return
        self._nc = await nats.connect(self._url)
        self._js = self._nc.jetstream()
        await ensure_memory_stream(self._js, get_memory_settings())

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
        self._nc = None
        self._js = None

    async def publish(self, payload: MemoryCommandPayload) -> None:
        """Publish ``payload`` to ``agent.memory.cmd.<payload.user_id>``."""
        if self._js is None:
            await self.connect()
        assert self._js is not None
        user_id = (payload.user_id or "").strip()
        if not user_id:
            msg = "MemoryCommandPayload.user_id is required for per-user routing"
            raise ValueError(msg)
        subject = memory_command_subject(user_id)
        body = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        await self._js.publish(subject, body)
