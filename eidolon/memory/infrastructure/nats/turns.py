"""Publish completed conversation turns to NATS JetStream."""

from __future__ import annotations

import json
from typing import Any

import nats
from eidolon_sdk.memory import ConversationTurnPayload, conversation_turn_subject

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class JetStreamTurnPublisher:
    """Connects to NATS and publishes ``ConversationTurnPayload`` per memory space."""

    def __init__(self, *, nats_url: str, stream_name: str) -> None:
        self._url = nats_url
        self._stream = stream_name
        self._nc: nats.NATS | None = None
        self._js: Any = None

    @classmethod
    def from_memory_settings(cls, settings: MemorySettings) -> "JetStreamTurnPublisher":
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

    async def publish_turn(self, payload: ConversationTurnPayload) -> None:
        """Publish ``payload`` to ``eidolon.memory.turn.<memory_space_id>``."""
        if self._js is None:
            await self.connect()
        assert self._js is not None
        memory_space_id = (payload.context.memory_space_id or "").strip()
        if not memory_space_id:
            msg = "ConversationTurnPayload.context.memory_space_id is required for routing"
            raise ValueError(msg)
        subject = conversation_turn_subject(memory_space_id)
        body = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        await self._js.publish(subject, body)
