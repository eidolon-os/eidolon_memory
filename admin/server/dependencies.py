"""FastAPI deps: settings, optional Bearer auth, JetStream publisher.

There is intentionally **no** cached MCP session here. Each /api endpoint
that needs MCP opens a fresh per-request session via ``mcp_call.call_user_mcp``
— admin is not on the LiveKit hot path, and per-request sessions remove an
entire class of stale-state bugs (agent restart, transport half-close).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.users import UserEntry
from eidolon.memory.infrastructure.nats.turns import JetStreamTurnPublisher

from user_registry import resolve_user_entry

_publisher: JetStreamTurnPublisher | None = None


def get_memory_settings_cached() -> MemorySettings:
    return get_memory_settings()


async def verify_admin_optional(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    import os

    expected = os.environ.get("EIDOLON_MEMORY_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    got = authorization[7:].strip()
    if got != expected:
        raise HTTPException(status_code=403, detail="invalid token")


def resolve_user(
    settings: MemorySettings,
    user_id: str | None,
) -> UserEntry:
    return resolve_user_entry(settings, user_id)


async def get_turn_publisher() -> JetStreamTurnPublisher:
    global _publisher
    if _publisher is None:
        _publisher = JetStreamTurnPublisher.from_memory_settings(get_memory_settings())
        await _publisher.connect()
    return _publisher


AdminAuth = Annotated[None, Depends(verify_admin_optional)]
SettingsDep = Annotated[MemorySettings, Depends(get_memory_settings_cached)]
TurnPublisherDep = Annotated[JetStreamTurnPublisher, Depends(get_turn_publisher)]
