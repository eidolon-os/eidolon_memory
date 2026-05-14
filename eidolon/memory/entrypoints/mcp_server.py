"""Eidolon-owned MCP read server for companion agents."""

from __future__ import annotations

from typing import Any

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.public_recall import (
    group_recall_context,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.config.memory_settings import get_memory_settings
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


def build_server():
    """Build the MCP server lazily so importing this module does not require mcp."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("eidolon-memory")

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
        settings = get_memory_settings()
        records = await search_all_wings_mcp_style(
            backend,
            settings,
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
        settings = get_memory_settings()
        records = await search_all_wings_mcp_style(
            backend,
            settings,
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
        settings = get_memory_settings()
        return {
            "backend": "mempalace-python",
            "backend_configured": True,
            "palace_path": str(resolve_palace_directory(settings)),
            "steward_mode": settings.steward.mode,
            "wings": [w.model_dump() for w in settings.wings],
        }

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
