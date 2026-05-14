"""Eidolon-owned MCP read server for companion agents."""

from __future__ import annotations

from typing import Any

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory
from eidolon.memory.domain.wire import MemoryWireRecord

_backend: MemPalacePythonBackend | None = None


async def _get_backend() -> MemPalacePythonBackend:
    global _backend
    if _backend is not None:
        return _backend
    settings = get_memory_settings()
    palace = str(resolve_palace_directory(settings))
    _backend = MemPalacePythonBackend(settings, palace)
    return _backend


def _rec_to_dict(rec: MemoryWireRecord) -> dict[str, Any]:
    return rec.model_dump(mode="json")


def _visible(rec: MemoryWireRecord, user_id: str) -> bool:
    if rec.metadata.get("wing") == "Wing_Privacy" or rec.user_id == "Wing_Privacy":
        return False
    privacy = str(rec.metadata.get("privacy", "")).lower()
    if privacy in {"private", "do_not_recall"}:
        return False
    meta_user = str(rec.metadata.get("user_id", ""))
    return not meta_user or meta_user == user_id


def _group_context(records: list[MemoryWireRecord]) -> str:
    groups: dict[str, list[str]] = {
        "个人事实与偏好": [],
        "关系": [],
        "情绪": [],
        "工作学习": [],
        "事件与生活": [],
    }
    for rec in records:
        kind = str(rec.metadata.get("memory_type", "")).lower()
        text = str(rec.value)
        if kind in {"profile", "preference", "health"}:
            groups["个人事实与偏好"].append(text)
        elif kind == "relationship":
            groups["关系"].append(text)
        elif kind == "emotion":
            groups["情绪"].append(text)
        elif kind == "work":
            groups["工作学习"].append(text)
        else:
            groups["事件与生活"].append(text)
    lines: list[str] = []
    for title, items in groups.items():
        if items:
            lines.append(f"{title}:")
            lines.extend(f"- {item}" for item in items[:4])
    return "\n".join(lines)


async def _search_all_wings(
    *,
    query: str,
    user_id: str,
    top_k: int,
    wing: str | None,
    room: str | None,
) -> list[MemoryWireRecord]:
    backend = await _get_backend()
    settings = get_memory_settings()
    wings = [wing] if wing else [w.id for w in settings.wings if w.id != "Wing_Privacy"]
    hits: list[MemoryWireRecord] = []
    for wing_id in wings:
        found = await backend.search(query, wing=wing_id, n_results=top_k, room=room)
        hits.extend(r for r in found if _visible(r, user_id))
    hits.sort(
        key=lambda r: (
            int(r.metadata.get("importance", 0) or 0),
            -float(r.metadata.get("score", 1.0) or 1.0),
        ),
        reverse=True,
    )
    return hits[:top_k]


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
        records = await _search_all_wings(
            query=query,
            user_id=user_id or "default",
            top_k=top_k,
            wing=wing,
            room=room,
        )
        return [_rec_to_dict(r) for r in records]

    @mcp.tool()
    async def eidolon_memory_recall_context(
        query: str,
        user_id: str,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Return both structured records and a compact context block."""
        records = await _search_all_wings(
            query=query,
            user_id=user_id or "default",
            top_k=top_k,
            wing=None,
            room=None,
        )
        return {
            "context": _group_context(records),
            "records": [_rec_to_dict(r) for r in records],
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
