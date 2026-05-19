"""FastAPI deps: per-user MCP sessions, JetStream publisher, optional Bearer auth."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, Request
from mcp.client.session import ClientSession

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.users import UserEntry
from eidolon.memory.infrastructure.nats.turns import JetStreamTurnPublisher

from mcp_sessions import UserMcpSessionManager
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


def get_mcp_manager(request: Request) -> UserMcpSessionManager:
    manager = getattr(request.app.state, "mcp_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="MCP session manager not ready; start eidolon-memory-agent / supervisor first",
        )
    return manager


def resolve_user(
    settings: MemorySettings,
    user_id: str | None,
) -> UserEntry:
    return resolve_user_entry(settings, user_id)


async def get_mcp_session(
    request: Request,
    settings: Annotated[MemorySettings, Depends(get_memory_settings_cached)],
    user_id: Annotated[str, Query(description="users.yaml id; selects agent_runner MCP port")],
    manager: Annotated[UserMcpSessionManager, Depends(get_mcp_manager)],
) -> ClientSession:
    entry = resolve_user(settings, user_id)
    try:
        return await manager.get_session(entry)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"cannot connect to agent_runner for user {entry.id!r} "
                f"on port {entry.port}: {exc}"
            ),
        ) from exc


async def get_turn_publisher() -> JetStreamTurnPublisher:
    global _publisher
    if _publisher is None:
        _publisher = JetStreamTurnPublisher.from_memory_settings(get_memory_settings())
        await _publisher.connect()
    return _publisher


AdminAuth = Annotated[None, Depends(verify_admin_optional)]
SettingsDep = Annotated[MemorySettings, Depends(get_memory_settings_cached)]
McpSessionDep = Annotated[ClientSession, Depends(get_mcp_session)]
TurnPublisherDep = Annotated[JetStreamTurnPublisher, Depends(get_turn_publisher)]
