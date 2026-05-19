"""Eidolon-owned MCP read server for companion agents (Streamable HTTP)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, TypeVar

from eidolon.memory.application.admin_visibility import admin_row_visible
from eidolon.memory.application.mempalace_hierarchy import build_mempalace_hierarchy_snapshot
from eidolon.memory.application.public_recall import (
    group_recall_context,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.application.runtime_warm import warm_palace_read_path
from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory
from eidolon.memory.domain.errors import MemoryBackendUnavailable
from eidolon.memory.infrastructure.chroma_refresh import (
    close_mempalace_palace,
    is_recoverable_db_error,
)
from eidolon.memory.infrastructure.palace_read_session import PalaceReadSession
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_read_session: PalaceReadSession | None = None

_T = TypeVar("_T")


def _palace_path(settings: MemorySettings | None = None) -> str:
    return str(resolve_palace_directory(settings or get_memory_settings()))


def get_read_session(settings: MemorySettings | None = None) -> PalaceReadSession:
    global _read_session
    if _read_session is None:
        mem = settings or get_memory_settings()
        _read_session = PalaceReadSession(mem, _palace_path(mem))
    return _read_session


def reset_read_session() -> None:
    """Clear process-local read session (tests / smoke)."""
    global _read_session
    if _read_session is not None:
        _read_session.close()
    _read_session = None


async def _mcp_with_backend(
    op: Callable[[Any], Awaitable[_T]],
    *,
    settings: MemorySettings | None = None,
) -> _T:
    """Run a backend operation with generation refresh and one DB recovery retry."""
    mem = settings or get_memory_settings()
    palace = _palace_path(mem)
    session = get_read_session(mem)
    last_exc: BaseException | None = None

    for attempt in range(2):
        await session.ensure_fresh()
        backend = await session.active_backend()
        try:
            return await op(backend)
        except MemoryBackendUnavailable as exc:
            last_exc = exc
            if attempt == 0 and is_recoverable_db_error(exc):
                log.warning(
                    "mcp_db_recoverable_retry",
                    error=str(exc),
                    palace=palace,
                    attempt=attempt,
                )
                close_mempalace_palace(palace)
                reset_read_session()
                await asyncio.sleep(1.5)
                session = get_read_session(mem)
                continue
            raise

    if last_exc is not None:
        raise last_exc
    msg = "mcp backend operation failed without exception"
    raise MemoryBackendUnavailable(msg)


async def _mcp_search(
    *,
    query: str,
    user_id: str,
    top_k: int,
    wing: str | None,
    room: str | None,
) -> list:
    settings = get_memory_settings()

    async def _run(backend: Any) -> list:
        return await search_all_wings_mcp_style(
            backend,
            settings,
            query=query,
            user_id=user_id or "default",
            top_k=top_k,
            wing=wing,
            room=room,
            for_voice=False,
            palace_path=_palace_path(settings),
        )

    return await _mcp_with_backend(_run, settings=settings)


def build_server(settings: MemorySettings | None = None):
    """Build the MCP server lazily so importing this module does not require mcp."""
    from mcp.server.fastmcp import FastMCP

    mem_settings = settings or get_memory_settings()
    cfg = mem_settings.mcp_http

    @asynccontextmanager
    async def _lifespan(_app: object):
        from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env

        apply_cpu_thread_env(mem_settings, role="mcp")
        palace = _palace_path(mem_settings)
        log.info("mcp_runtime_warm_start", palace=palace)
        try:
            await warm_palace_read_path(mem_settings, palace, role="mcp")
        except Exception as exc:
            log.warning("mcp_runtime_warm_failed", error=str(exc))
        yield
        reset_read_session()

    mcp = FastMCP(
        "eidolon-memory",
        host=cfg.host,
        port=cfg.port,
        streamable_http_path=cfg.path if cfg.path.startswith("/") else f"/{cfg.path}",
        stateless_http=cfg.stateless_http,
        lifespan=_lifespan,
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
        records = await _mcp_search(
            query=query,
            user_id=user_id,
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
        records = await _mcp_search(
            query=query,
            user_id=user_id,
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
        session = get_read_session(mem_settings)
        return {
            "backend": "mempalace-python",
            "backend_configured": True,
            "palace_path": _palace_path(mem_settings),
            "steward_mode": mem_settings.steward.mode,
            "mcp_transport": "streamable-http",
            "mcp_http_url": mem_settings.mcp_http.base_url(),
            "wings": [w.model_dump() for w in mem_settings.wings],
            "palace_generation": session.current_generation(),
        }

    @mcp.tool()
    async def eidolon_memory_list(
        tenant_id: str = "",
        limit: int = 500,
        offset: int = 0,
        include_private: bool = False,
    ) -> dict[str, Any]:
        """Paginated listing aligned with Admin scan (omit tenant for full palace)."""
        tid = tenant_id.strip()
        lim = max(1, min(limit, 5000))
        off = max(0, offset)

        async def _run(backend: Any) -> dict[str, Any]:
            rows = await backend.get_all(tid, limit=lim, offset=off)
            filtered = [r for r in rows if admin_row_visible(r, include_private=include_private)]
            return {
                "records": [wire_record_to_public_dict(r) for r in filtered],
                "total_hint": len(filtered),
            }

        return await _mcp_with_backend(_run, settings=mem_settings)

    @mcp.tool()
    async def eidolon_memory_delete(key: str, user_id: str = "") -> dict[str, Any]:
        """Delete a drawer by MemPalace id (dev/ops only; production writes go via Worker)."""
        del user_id
        if not key.startswith("drawer_"):
            msg = "key must be a MemPalace drawer_* id"
            raise ValueError(msg)

        async def _run(backend: Any) -> dict[str, Any]:
            await backend.delete("", key)
            return {"status": "deleted", "key": key}

        return await _mcp_with_backend(_run, settings=mem_settings)

    @mcp.tool()
    async def eidolon_memory_hierarchy_snapshot(
        max_records: int = 8000,
        max_drawers_per_room: int = 48,
    ) -> dict[str, Any]:
        """Return wing→room→drawer tree snapshot (bounded scan) matching Admin hierarchy."""
        mr = max(50, min(max_records, 50_000))
        md = max(4, min(max_drawers_per_room, 400))

        async def _run(backend: Any) -> dict[str, Any]:
            return await build_mempalace_hierarchy_snapshot(
                backend,
                mem_settings,
                max_records=mr,
                max_drawers_per_room=md,
            )

        return await _mcp_with_backend(_run, settings=mem_settings)

    return mcp


def main() -> None:
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
