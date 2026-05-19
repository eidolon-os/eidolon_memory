"""JetStream stream configuration shared by agent_runner subscribers and publishers (D1)."""

from __future__ import annotations

from typing import Any

from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.bus.subjects import conversation_turn_stream_pattern


def stream_config_for_settings(settings: MemorySettings) -> StreamConfig:
    nats = settings.nats
    return StreamConfig(
        name=nats.stream,
        subjects=[conversation_turn_stream_pattern()],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=nats.stream_max_age_seconds,
        max_msgs=nats.stream_max_msgs,
        max_bytes=nats.stream_max_bytes,
        discard=DiscardPolicy.OLD,
    )


async def ensure_memory_stream(js: Any, settings: MemorySettings) -> None:
    cfg = stream_config_for_settings(settings)
    try:
        await js.stream_info(settings.nats.stream)
        try:
            await js.update_stream(cfg)
        except Exception:
            pass
    except Exception:
        await js.add_stream(cfg)
