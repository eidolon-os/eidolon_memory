"""Per-user MCP HTTP sessions (one control-plane port per agent_runner)."""

from __future__ import annotations

from contextlib import AsyncExitStack

from mcp.client.session import ClientSession

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.users import UserEntry
from eidolon.memory.support.logging import get_logger

from mcp_client import mcp_http_session, mcp_http_url
from user_registry import list_enabled_users

log = get_logger(__name__)


class UserMcpSessionManager:
    def __init__(self, settings: MemorySettings) -> None:
        self._settings = settings
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}

    async def open(self) -> None:
        for entry in list_enabled_users(self._settings):
            await self._connect_user(entry)

    async def close(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()

    async def _connect_user(self, entry: UserEntry) -> None:
        url = mcp_http_url(self._settings, port=entry.port)
        try:
            cm = mcp_http_session(url, settings=self._settings, connect_attempts=8)
            session = await self._stack.enter_async_context(cm)
            self._sessions[entry.id] = session
            log.info("admin_mcp_connected", user_id=entry.id, url=url)
        except Exception as exc:
            log.warning("admin_mcp_connect_failed", user_id=entry.id, url=url, error=str(exc))

    async def get_session(self, entry: UserEntry) -> ClientSession:
        session = self._sessions.get(entry.id)
        if session is not None:
            return session
        url = mcp_http_url(self._settings, port=entry.port)
        cm = mcp_http_session(url, settings=self._settings, connect_attempts=4)
        session = await self._stack.enter_async_context(cm)
        self._sessions[entry.id] = session
        return session
