"""Control-plane MCP tool factory (D1).

Each ``agent_runner`` process hosts its own FastMCP instance bound to the
loopback control-plane port. There is no longer a standalone MCP server entrypoint;
tools share the agent runner's ``LockedBackend`` (single PersistentClient per palace,
single ``asyncio.Lock`` for read+write).
"""

from __future__ import annotations

from typing import Any

from eidolon.memory.application.admin_visibility import admin_row_visible
from eidolon.memory.application.mempalace_hierarchy import build_mempalace_hierarchy_snapshot
from eidolon.memory.application.public_recall import (
    group_recall_context,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def build_control_plane_mcp(
    backend: MemoryBackend,
    settings: MemorySettings,
    *,
    user_id: str,
    palace_path: str,
    host: str,
    port: int,
    lifespan: Any = None,
):
    """Construct a FastMCP server bound to ``(host, port)`` for one user's runner.

    Tools run in-process against the supplied ``backend`` (typically a
    ``LockedBackend`` wrapping the user's ``MemPalacePythonBackend``). The
    same instance is also called directly by ``LiveKitRecallService`` — both
    paths share the lock.
    """
    from mcp.server.fastmcp import FastMCP

    cfg = settings.mcp_http
    streamable_path = cfg.path if cfg.path.startswith("/") else f"/{cfg.path}"

    mcp_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "streamable_http_path": streamable_path,
        "stateless_http": cfg.stateless_http,
    }
    if lifespan is not None:
        mcp_kwargs["lifespan"] = lifespan

    mcp = FastMCP(f"eidolon-memory-{user_id}", **mcp_kwargs)

    @mcp.tool()
    async def eidolon_memory_search(
        query: str,
        top_k: int = 5,
        wing: str | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search this user's memory; ``user_id`` is bound by the agent runner."""
        records = await search_all_wings_mcp_style(
            backend,
            settings,
            query=query,
            user_id=user_id,
            top_k=top_k,
            wing=wing,
            room=room,
            for_voice=False,
            palace_path=palace_path,
        )
        return [wire_record_to_public_dict(r) for r in records]

    @mcp.tool()
    async def eidolon_memory_recall_context(
        query: str,
        top_k: int = 5,
        voice: bool = False,
    ) -> dict[str, Any]:
        """Return both structured records and a grouped context block.

        ``voice=True`` enables the LiveKit hot-path optimizations
        (shared query embedding across wings, skip closets); the LiveKit
        pipeline calls the same code via ``LiveKitRecallService.recall_context``.
        """
        records = await search_all_wings_mcp_style(
            backend,
            settings,
            query=query,
            user_id=user_id,
            top_k=top_k,
            wing=None,
            room=None,
            for_voice=voice,
            palace_path=palace_path,
        )
        return {
            "context": group_recall_context(records),
            "records": [wire_record_to_public_dict(r) for r in records],
        }

    @mcp.tool()
    async def eidolon_memory_status() -> dict[str, Any]:
        """Report this agent runner's memory service status."""
        return {
            "backend": "mempalace-python",
            "user_id": user_id,
            "palace_path": palace_path,
            "steward_mode": settings.steward.mode,
            "mcp_transport": "streamable-http",
            "mcp_http_url": settings.mcp_http.base_url(port=port),
            "wings": [w.model_dump() for w in settings.wings],
        }

    @mcp.tool()
    async def eidolon_memory_list(
        limit: int = 500,
        offset: int = 0,
        include_private: bool = False,
    ) -> dict[str, Any]:
        """Paginated listing of this user's drawers (Admin / IDE)."""
        lim = max(1, min(limit, 5000))
        off = max(0, offset)
        rows = await backend.get_all(user_id, limit=lim, offset=off)
        filtered = [r for r in rows if admin_row_visible(r, include_private=include_private)]
        return {
            "records": [wire_record_to_public_dict(r) for r in filtered],
            "total_hint": len(filtered),
        }

    @mcp.tool()
    async def eidolon_memory_hierarchy_snapshot(
        max_records: int = 8000,
        max_drawers_per_room: int = 48,
    ) -> dict[str, Any]:
        """Return wing→room→drawer tree snapshot (bounded scan)."""
        mr = max(50, min(max_records, 50_000))
        md = max(4, min(max_drawers_per_room, 400))
        return await build_mempalace_hierarchy_snapshot(
            backend,
            settings,
            palace_path=palace_path,
            max_records=mr,
            max_drawers_per_room=md,
        )

    return mcp
