"""Eidolon-owned MCP read server for companion agents (Streamable HTTP)."""

from __future__ import annotations

from typing import Any

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.admin_visibility import admin_row_visible
from eidolon.memory.application.mempalace_hierarchy import build_mempalace_hierarchy_snapshot
from eidolon.memory.application.public_recall import (
    group_recall_context,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory

_backend: MemPalacePythonBackend | None = None


async def _get_backend() -> MemPalacePythonBackend:
    global _backend
    if _backend is not None:
        return _backend
    settings = get_memory_settings()
    palace = str(resolve_palace_directory(settings))
    _backend = MemPalacePythonBackend(settings, palace)
    return _backend


def reset_backend_cache() -> None:
    """Clear the process-local backend singleton (tests / smoke)."""
    global _backend
    _backend = None


def build_server(settings: MemorySettings | None = None):
    """Build the MCP server lazily so importing this module does not require mcp."""
    from mcp.server.fastmcp import FastMCP

    cfg = (settings or get_memory_settings()).mcp_http
    mcp = FastMCP(
        "eidolon-memory",
        host=cfg.host,
        port=cfg.port,
        streamable_http_path=cfg.path if cfg.path.startswith("/") else f"/{cfg.path}",
        stateless_http=cfg.stateless_http,
    )

    @mcp.tool()
    async def eidolon_memory_search(
        query: str,
        user_id: str,
        top_k: int = 5,
        wing: str | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search Eidolon memory without exposing MemPalace tool details."""
        backend = await _get_backend()
        mem_settings = get_memory_settings()
        records = await search_all_wings_mcp_style(
            backend,
            mem_settings,
            query=query,
            user_id=user_id or "default",
            top_k=top_k,
            wing=wing,
            room=room,
        )
        return [wire_record_to_public_dict(r) for r in records]

    @mcp.tool()
    async def eidolon_memory_recall_context(
        query: str,
        user_id: str,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Return both structured records and a compact context block."""
        backend = await _get_backend()
        mem_settings = get_memory_settings()
        records = await search_all_wings_mcp_style(
            backend,
            mem_settings,
            query=query,
            user_id=user_id or "default",
            top_k=top_k,
            wing=None,
            room=None,
        )
        return {
            "context": group_recall_context(records),
            "records": [wire_record_to_public_dict(r) for r in records],
        }

    @mcp.tool()
    async def eidolon_memory_status() -> dict[str, Any]:
        """Report memory server configuration."""
        mem_settings = get_memory_settings()
        return {
            "backend": "mempalace-python",
            "backend_configured": True,
            "palace_path": str(resolve_palace_directory(mem_settings)),
            "steward_mode": mem_settings.steward.mode,
            "mcp_transport": "streamable-http",
            "mcp_http_url": mem_settings.mcp_http.base_url(),
            "wings": [w.model_dump() for w in mem_settings.wings],
        }

    @mcp.tool()
    async def eidolon_memory_list(
        tenant_id: str = "",
        limit: int = 500,
        offset: int = 0,
        include_private: bool = False,
    ) -> dict[str, Any]:
        """Paginated listing aligned with Admin scan (omit tenant for full palace)."""
        backend = await _get_backend()
        tid = tenant_id.strip()
        lim = max(1, min(limit, 5000))
        off = max(0, offset)
        rows = await backend.get_all(tid, limit=lim, offset=off)
        filtered = [r for r in rows if admin_row_visible(r, include_private=include_private)]
        return {
            "records": [wire_record_to_public_dict(r) for r in filtered],
            "total_hint": len(filtered),
        }

    @mcp.tool()
    async def eidolon_memory_delete(key: str, user_id: str = "") -> dict[str, Any]:
        """Delete a drawer by MemPalace id (expects ``drawer_*`` prefix)."""
        del user_id
        if not key.startswith("drawer_"):
            msg = "key must be a MemPalace drawer_* id"
            raise ValueError(msg)
        backend = await _get_backend()
        await backend.delete("", key)
        return {"status": "deleted", "key": key}

    @mcp.tool()
    async def eidolon_memory_hierarchy_snapshot(
        max_records: int = 8000,
        max_drawers_per_room: int = 48,
    ) -> dict[str, Any]:
        """Return wing→room→drawer tree snapshot (bounded scan) matching Admin hierarchy."""
        backend = await _get_backend()
        mem_settings = get_memory_settings()
        mr = max(50, min(max_records, 50_000))
        md = max(4, min(max_drawers_per_room, 400))
        return await build_mempalace_hierarchy_snapshot(
            backend,
            mem_settings,
            max_records=mr,
            max_drawers_per_room=md,
        )

    return mcp


def main() -> None:
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
