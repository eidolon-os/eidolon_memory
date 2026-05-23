"""Streamable HTTP MCP client (Admin-local; replaces removed eidolon mcp_http_client)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings


def _create_local_mcp_http_client(headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "timeout": httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
        "trust_env": False,
    }
    if headers:
        kwargs["headers"] = headers
    return httpx.AsyncClient(**kwargs)


def mcp_http_url(settings: MemorySettings, *, port: int) -> str:
    return settings.mcp_http.base_url(port=port)


def _unwrap_fastmcp_structured(payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


def decode_call_tool_json(result: types.CallToolResult) -> Any:
    if result.isError:
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
        raise RuntimeError(" | ".join(parts) if parts else "MCP tool error")
    if result.structuredContent is not None:
        return _unwrap_fastmcp_structured(result.structuredContent)
    for block in result.content:
        if isinstance(block, types.TextContent):
            text = block.text.strip()
            if not text:
                continue
            return _unwrap_fastmcp_structured(json.loads(text))
    return None


@asynccontextmanager
async def mcp_http_session(
    url: str,
    *,
    settings: MemorySettings | None = None,
    connect_attempts: int = 8,
    connect_delay_seconds: float = 0.5,
):
    cfg = settings or get_memory_settings()
    headers = cfg.mcp_http.auth_headers()
    last_exc: Exception | None = None
    for attempt in range(connect_attempts):
        try:
            client = _create_local_mcp_http_client(headers or None)
            async with client:
                async with streamable_http_client(url, http_client=client) as (
                    read,
                    write,
                    _get_session_id,
                ):
                    del _get_session_id
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
                        return
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= connect_attempts:
                break
            await asyncio.sleep(connect_delay_seconds)
    msg = f"failed to connect to MCP HTTP at {url}"
    raise RuntimeError(msg) from last_exc


async def call_tool_json(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    result = await session.call_tool(name, arguments=arguments or {})
    return decode_call_tool_json(result)


async def probe_mcp_http(url: str, *, settings: MemorySettings | None = None) -> bool:
    cfg = settings or get_memory_settings()
    try:
        async with mcp_http_session(
            url,
            settings=cfg,
            connect_attempts=1,
        ):
            return True
    except Exception:
        return False
