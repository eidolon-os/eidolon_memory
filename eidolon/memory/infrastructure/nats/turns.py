"""Publish completed conversation turns to NATS JetStream (D1: per-user subject)."""

from __future__ import annotations

import json
from typing import Any

import nats

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.infrastructure.bus.subjects import conversation_turn_subject
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class JetStreamTurnPublisher:
    """Connects to NATS and publishes ``ConversationTurnPayload`` per user_id subject."""

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
        from eidolon.memory.config.memory_settings import get_memory_settings

        await ensure_memory_stream(self._js, get_memory_settings())

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
        self._nc = None
        self._js = None

    async def publish_turn(self, payload: ConversationTurnPayload) -> None:
        """Publish ``payload`` to ``agent.memory.conversation.turn.<payload.user_id>``."""
        if self._js is None:
            await self.connect()
        assert self._js is not None
        user_id = (payload.user_id or "").strip()
        if not user_id:
            msg = "ConversationTurnPayload.user_id is required for per-user routing"
            raise ValueError(msg)
        subject = conversation_turn_subject(user_id)
        body = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        await self._js.publish(subject, body)
