"""Read-only Palace inventory and offline snapshot-manifest helpers."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SQLITE_COUNT_TABLES = {
    "chroma.sqlite3": ("collections", "segments", "embeddings", "embeddings_queue"),
    "knowledge_graph.sqlite3": ("entities", "triples", "entity_mentions"),
}


def memory_space_id_from_storage_name(storage_name: str) -> str | None:
    """Decode an SDK ``b64_`` storage name without accepting malformed input."""

    if not storage_name.startswith("b64_"):
        return None
    token = storage_name[4:]
    try:
        padding = "=" * (-len(token) % 4)
        value = base64.urlsafe_b64decode(token + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    canonical = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    return value if canonical == token else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_summary(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"quick_check": "not_run", "counts": {}}
    try:
        # Deep manifests are restricted to stopped Palaces or immutable
        # snapshot copies. ``immutable=1`` prevents SQLite from trying to
        # create journal/SHM files in a read-only production data directory.
        connection = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1",
            uri=True,
            timeout=10.0,
        )
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
            result["quick_check"] = str(row[0]) if row else "no_result"
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table in _SQLITE_COUNT_TABLES.get(path.name, ()):
                if table in available:
                    count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                    result["counts"][table] = int(count[0]) if count else 0
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def build_palace_manifest(palace_path: Path, *, deep: bool = False) -> dict[str, Any]:
    """Build metadata for one Palace; ``deep`` is for a stopped/snapshot copy.

    Deep mode hashes every regular file and opens SQLite databases read-only.
    It must not be treated as a consistent backup while an agent still owns
    the Palace; callers record that operational assertion separately.
    """

    palace_path = Path(palace_path).expanduser().resolve()
    files = sorted(path for path in palace_path.rglob("*") if path.is_file())
    manifest: dict[str, Any] = {
        "storage_name": palace_path.name,
        "memory_space_id": memory_space_id_from_storage_name(palace_path.name),
        "path": str(palace_path),
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "artifacts": {},
    }
    for name in ("chroma.sqlite3", "knowledge_graph.sqlite3"):
        path = palace_path / name
        manifest["artifacts"][name] = {
            "exists": path.is_file(),
            "size": path.stat().st_size if path.is_file() else 0,
        }

    if deep:
        manifest["files"] = [
            {
                "path": str(path.relative_to(palace_path)),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in files
        ]
        manifest["sqlite"] = {
            name: _sqlite_summary(palace_path / name)
            for name in _SQLITE_COUNT_TABLES
            if (palace_path / name).is_file()
        }
    return manifest


def build_palaces_inventory(palaces_root: Path, *, deep: bool = False) -> dict[str, Any]:
    """Build a deterministic inventory for every non-hidden Palace directory."""

    palaces_root = Path(palaces_root).expanduser().resolve()
    palaces = []
    if palaces_root.is_dir():
        palaces = [
            build_palace_manifest(path, deep=deep)
            for path in sorted(palaces_root.iterdir())
            if path.is_dir() and not path.name.startswith(".")
        ]
    try:
        mempalace_version = importlib.metadata.version("mempalace")
    except importlib.metadata.PackageNotFoundError:
        mempalace_version = "not-installed"
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "palaces_root": str(palaces_root),
        "mempalace_version": mempalace_version,
        "deep_offline_manifest": deep,
        "palace_count": len(palaces),
        "palaces": palaces,
    }
