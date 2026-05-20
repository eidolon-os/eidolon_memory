"""Per-request MCP call helper (no session caching).

Each admin endpoint opens a fresh Streamable HTTP MCP session, calls the
target tool, closes the session. Cost on localhost is ~10-25 ms per request
— irrelevant for admin (no LiveKit budget) and pays for itself by
eliminating an entire class of stale-session bugs (agent restart, transport
half-closed, etc.).

The memory hot path (LiveKit) does NOT use this — it talks to its
own ``LockedBackend`` / ``LockedKnowledgeGraph`` in-process.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from eidolon.memory.config.memory_settings import MemorySettings

from mcp_client import call_tool_json, mcp_http_session, mcp_http_url
from user_registry import resolve_user_entry


async def call_user_mcp(
    settings: MemorySettings,
    user_id: str,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    connect_attempts: int = 3,
) -> Any:
    """Open a fresh per-request session against the user's agent_runner MCP."""
    entry = resolve_user_entry(settings, user_id)
    url = mcp_http_url(settings, port=entry.port)
    try:
        async with mcp_http_session(
            url, settings=settings, connect_attempts=connect_attempts
        ) as session:
            return await call_tool_json(session, tool, args or {})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"agent_runner for user {entry.id!r} unreachable on port "
                f"{entry.port} ({tool!r}): {exc}"
            ),
        ) from exc


async def list_user_mcp_tools(settings: MemorySettings, user_id: str) -> list[dict[str, Any]]:
    """Per-request list_tools — mirrors call_user_mcp but uses the raw API."""
    entry = resolve_user_entry(settings, user_id)
    url = mcp_http_url(settings, port=entry.port)
    try:
        async with mcp_http_session(url, settings=settings, connect_attempts=2) as session:
            result = await session.list_tools()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"agent_runner for user {entry.id!r} unreachable on port "
                f"{entry.port} (list_tools): {exc}"
            ),
        ) from exc
    out: list[dict[str, Any]] = []
    for t in getattr(result, "tools", []) or []:
        schema = getattr(t, "inputSchema", None)
        if not isinstance(schema, dict):
            if hasattr(schema, "model_dump"):
                schema = schema.model_dump(mode="json")
            elif schema is None:
                schema = {}
            else:
                schema = dict(getattr(schema, "__dict__", {}) or {})
        out.append(
            {
                "name": str(getattr(t, "name", "")),
                "description": str(getattr(t, "description", "") or ""),
                "input_schema": schema,
            }
        )
    return out
