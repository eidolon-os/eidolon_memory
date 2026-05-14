"""Subprocess MCP client for Admin façade (tests the same MCP stdio stack as IDE hosts)."""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client


def decode_call_tool_json(result: types.CallToolResult) -> Any:
    """Turn ``tools/call`` result into JSON-like Python values."""
    if result.isError:
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
        raise RuntimeError(" | ".join(parts) if parts else "MCP tool error")
    if result.structuredContent is not None:
        return result.structuredContent
    for block in result.content:
        if isinstance(block, types.TextContent):
            text = block.text.strip()
            if not text:
                continue
            return json.loads(text)
    return None


@asynccontextmanager
async def eidolon_memory_mcp_stdio_session(*, cwd: str | None = None):
    """Spawn memory MCP subprocess; yield initialized ``ClientSession``."""
    env = {**get_default_environment(), **dict(os.environ)}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "eidolon.memory.entrypoints.mcp_server"],
        env=env,
        cwd=cwd,
    )
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def call_tool_json(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    result = await session.call_tool(name, arguments=arguments or {})
    return decode_call_tool_json(result)
