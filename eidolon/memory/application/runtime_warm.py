"""Shared runtime warmup for MCP and LiveKit processes."""

from __future__ import annotations

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.chroma_refresh import ensure_sqlite_wal
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


async def warm_palace_read_path(
    settings: MemorySettings,
    palace_path: str,
    *,
    role: str = "default",
) -> None:
    """Load ONNX, closets, and dry-run search on voice wings (blocking, call from startup)."""
    import asyncio

    from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env

    apply_cpu_thread_env(settings, role=role)  # type: ignore[arg-type]
    await asyncio.to_thread(_warm_sync, settings, palace_path)


def _warm_sync(settings: MemorySettings, palace_path: str) -> None:
    from pathlib import Path

    from mempalace.embedding import get_embedding_function
    from mempalace.palace import get_closets_collection
    from mempalace.searcher import search_memories

    sqlite = Path(palace_path) / "chroma.sqlite3"
    if sqlite.is_file():
        mode = ensure_sqlite_wal(str(sqlite))
        log.info("chroma_sqlite_pragma", **mode)

    log.info("runtime_warm_embedding_start")
    ef = get_embedding_function()
    ef(["eidolon memory warmup"])

    get_closets_collection(palace_path, create=True)

    wings = settings.recall.voice_wings or [
        w.id for w in settings.wings if w.id != "Wing_Privacy"
    ]
    if not wings:
        wings = [settings.wings[0].id]

    for wing_id in wings:
        data = search_memories(
            "warmup",
            palace_path=palace_path,
            wing=wing_id,
            n_results=1,
        )
        if isinstance(data, dict) and data.get("error"):
            log.warning("runtime_warm_search_failed", wing=wing_id, error=data.get("error"))
        else:
            log.info("runtime_warm_search_ok", wing=wing_id)

    log.info("runtime_warm_complete", palace=palace_path, wings=len(wings))
