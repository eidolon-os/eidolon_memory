"""Shared Chroma / MemPalace cache invalidation helpers."""

from __future__ import annotations


def is_transient_index_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "error finding id" in text or "error executing plan" in text


def is_database_locked_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database is locked" in repr(exc).lower()


def pop_mempalace_client_cache(palace_path: str) -> None:
    """Drop per-palace backend cache without closing PersistentClient (lighter than close)."""
    try:
        from mempalace.backends.chroma import ChromaBackend
        from mempalace.palace import _DEFAULT_BACKEND
    except ImportError:
        return
    if isinstance(_DEFAULT_BACKEND, ChromaBackend):
        _DEFAULT_BACKEND._clients.pop(palace_path, None)
        _DEFAULT_BACKEND._freshness.pop(palace_path, None)


def close_mempalace_palace(palace_path: str) -> None:
    """Full close of palace handles (MCP transient recovery / staging rebuild)."""
    try:
        from mempalace.backends.chroma import ChromaBackend
        from mempalace.palace import _DEFAULT_BACKEND
    except ImportError:
        return
    if isinstance(_DEFAULT_BACKEND, ChromaBackend):
        _DEFAULT_BACKEND.close_palace(palace_path)
    try:
        from chromadb.api.client import SharedSystemClient

        clear_system_cache = getattr(SharedSystemClient, "clear_system_cache", None)
        if callable(clear_system_cache):
            clear_system_cache()
    except Exception:
        pass


def ensure_sqlite_wal(chroma_sqlite: str) -> dict[str, str]:
    """Best-effort WAL on chroma.sqlite3; returns PRAGMA journal_mode result."""
    import sqlite3
    from pathlib import Path

    p = Path(chroma_sqlite)
    if not p.is_file():
        return {"journal_mode": "missing"}
    conn = sqlite3.connect(str(p), timeout=5.0)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
        return {"journal_mode": str(mode[0]) if mode else "unknown"}
    finally:
        conn.close()
