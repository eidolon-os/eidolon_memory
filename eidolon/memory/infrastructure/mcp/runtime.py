"""Long-lived MCP stdio session for MemPalace tool calls."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from eidolon.memory.infrastructure.mcp.config import McpServerLaunchConfig
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def _tool_result_to_json(result: Any) -> Any:
    if result.structuredContent is not None:
        return result.structuredContent
    from mcp.types import TextContent

    chunks: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            chunks.append(block.text)
    if not chunks:
        return None
    blob = "\n".join(chunks).strip()
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return {"raw": blob}


class MemPalaceMcpRuntime:
    """Holds one ``ClientSession`` over stdio (spawn MemPalace MCP server)."""

    def __init__(self, launch: McpServerLaunchConfig) -> None:
        if not launch.is_configured():
            msg = "MemPalace MCP is not configured (set EIDOLON_MEMORY_MCP_COMMAND)"
            raise ValueError(msg)
        self._launch = launch
        self._stack: AsyncExitStack | None = None
        self._session: Any = None

    @property
    def session(self) -> Any:
        if self._session is None:
            msg = "MemPalaceMcpRuntime.start() was not called"
            raise RuntimeError(msg)
        return self._session

    async def start(self) -> None:
        if self._session is not None:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self._launch.command,
            args=list(self._launch.args),
            env=self._launch.env,
            cwd=self._launch.cwd,
        )
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        sess_cm = ClientSession(read, write)
        session = await stack.enter_async_context(sess_cm)
        await session.initialize()
        self._stack = stack
        self._session = session
        log.info("mcp_runtime_started", command=self._launch.command)

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
        log.info("mcp_runtime_stopped")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        read_timeout_seconds: float | None = None,
    ) -> Any:
        import datetime

        timeout = None
        if read_timeout_seconds is not None:
            timeout = datetime.timedelta(seconds=read_timeout_seconds)
        result = await self.session.call_tool(
            name,
            arguments or {},
            read_timeout_seconds=timeout,
        )
        if result.isError:
            msg = f"MCP tool {name!r} failed"
            raise RuntimeError(msg)
        return _tool_result_to_json(result)
