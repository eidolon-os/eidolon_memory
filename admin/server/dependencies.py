"""FastAPI deps: MCP stdio session, JetStream publisher, optional Bearer auth."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from mcp.client.session import ClientSession

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.infrastructure.nats.turns import JetStreamTurnPublisher

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


def get_mcp_session(request: Request) -> ClientSession:
    session = getattr(request.app.state, "mcp_session", None)
    if session is None:
        raise HTTPException(status_code=503, detail="MCP client session is not ready")
    return session


async def get_turn_publisher() -> JetStreamTurnPublisher:
    """Lazily connect a JetStream publisher (Admin writes go through the worker pipeline)."""
    global _publisher
    if _publisher is None:
        _publisher = JetStreamTurnPublisher.from_memory_settings(get_memory_settings())
        await _publisher.connect()
    return _publisher


AdminAuth = Annotated[None, Depends(verify_admin_optional)]
SettingsDep = Annotated[MemorySettings, Depends(get_memory_settings_cached)]
McpSessionDep = Annotated[ClientSession, Depends(get_mcp_session)]
TurnPublisherDep = Annotated[JetStreamTurnPublisher, Depends(get_turn_publisher)]
