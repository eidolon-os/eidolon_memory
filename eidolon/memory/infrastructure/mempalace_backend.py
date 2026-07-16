"""MemPalace backend selection helpers.

Keeps Eidolon deployment code from assuming every palace is backed by
Chroma's local ``chroma.sqlite3`` file. MemPalace still owns the storage
contract; this module only centralizes process environment and artifact
checks for the supported backends we may run in development.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.embedding_model_dir import (
    apply_local_embedding_model_dir_from_env,
)

SUPPORTED_MEMPALACE_BACKENDS = frozenset({"chroma", "qdrant", "pgvector", "sqlite_exact"})


def selected_mempalace_backend(settings: MemorySettings) -> str:
    backend = (settings.mempalace.backend or "chroma").strip().lower()
    if backend not in SUPPORTED_MEMPALACE_BACKENDS:
        available = ", ".join(sorted(SUPPORTED_MEMPALACE_BACKENDS))
        raise ValueError(f"unsupported mempalace backend {backend!r}; available: {available}")
    return backend


def mempalace_backend_env(
    settings: MemorySettings,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment with MemPalace backend selection applied."""
    env = dict(base or os.environ)
    backend = selected_mempalace_backend(settings)
    env["MEMPALACE_BACKEND"] = backend

    embedding_model = settings.mempalace.embedding_model.strip().lower()
    if embedding_model:
        env["MEMPALACE_EMBEDDING_MODEL"] = embedding_model
    embedding_device = settings.mempalace.embedding_device.strip().lower()
    if embedding_device:
        env["MEMPALACE_EMBEDDING_DEVICE"] = embedding_device
    embedding_model_dir = settings.mempalace.embedding_model_dir.strip()
    if embedding_model_dir:
        env["MEMPALACE_EMBEDDING_MODEL_DIR"] = str(Path(embedding_model_dir).expanduser())
    if settings.mempalace.embedding_threads > 0:
        env["MEMPALACE_EMBEDDING_THREADS"] = str(settings.mempalace.embedding_threads)

    if backend == "qdrant":
        if settings.mempalace.qdrant_url:
            env["MEMPALACE_QDRANT_URL"] = settings.mempalace.qdrant_url
        if settings.mempalace.qdrant_namespace:
            env["MEMPALACE_QDRANT_NAMESPACE"] = settings.mempalace.qdrant_namespace
        if settings.mempalace.qdrant_timeout_seconds > 0:
            env["MEMPALACE_QDRANT_TIMEOUT"] = str(settings.mempalace.qdrant_timeout_seconds)
        api_key = settings.mempalace.resolve_qdrant_api_key()
        if api_key:
            env["MEMPALACE_QDRANT_API_KEY"] = api_key

    return env


def apply_mempalace_backend_env(settings: MemorySettings) -> None:
    """Apply backend selection to the current process."""
    env = mempalace_backend_env(settings)
    for key, value in env.items():
        if key.startswith("MEMPALACE_"):
            os.environ[key] = value
    apply_local_embedding_model_dir_from_env()


def backend_artifact_path(palace_path: Path, backend: str) -> Path:
    if backend == "chroma":
        return palace_path / "chroma.sqlite3"
    if backend == "qdrant":
        return palace_path / "qdrant_backend.json"
    if backend == "pgvector":
        return palace_path / "pgvector_backend.json"
    if backend == "sqlite_exact":
        return palace_path / "sqlite_exact.sqlite3"
    raise ValueError(f"unsupported mempalace backend {backend!r}")


def backend_is_initialized(palace_path: Path, backend: str) -> bool:
    return backend_artifact_path(palace_path, backend).is_file()


def vector_sqlite_integrity_targets(palace_path: Path, backend: str) -> list[tuple[str, Path]]:
    """Return local vector-store SQLite files that should pass integrity_check."""
    if backend == "chroma":
        return [("chroma", palace_path / "chroma.sqlite3")]
    if backend == "sqlite_exact":
        return [("sqlite_exact", palace_path / "sqlite_exact.sqlite3")]
    return []
