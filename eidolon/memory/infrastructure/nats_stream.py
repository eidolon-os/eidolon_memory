"""JetStream stream configuration shared by agent_runner subscribers and publishers (D1)."""

from __future__ import annotations

from typing import Any

from eidolon_memory_contracts import all_memory_stream_patterns
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

from eidolon.memory.config.memory_settings import MemorySettings


def stream_config_for_settings(settings: MemorySettings) -> StreamConfig:
    nats = settings.nats
    return StreamConfig(
        name=nats.stream,
        # KG plan §3.4: stream binds turn + command subjects so admin writes
        # share JetStream durability + replay with chat turns.
        subjects=all_memory_stream_patterns(),
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
