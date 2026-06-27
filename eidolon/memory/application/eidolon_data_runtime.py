"""Runtime composition between ``eidolon_data`` and eidolon_memory.

``eidolon_data`` owns the sovereignty schema and defines ``MemoryEnginePort``.
This module lives in ``eidolon_memory`` because only the memory project should
know how to construct MemPalace-backed implementations of that port.
"""

from __future__ import annotations

from pathlib import Path

from eidolon_data import DataSettings, DataStore

from eidolon.memory.adapters.eidolon_data_engine import EidolonDataMemoryEngine
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.config import (
    MemorySettings,
    get_memory_settings,
    resolve_palace_for_memory_space,
)
from eidolon.memory.domain.ports import MemoryBackend


def build_eidolon_data_memory_engine(
    *,
    backend: MemoryBackend | None = None,
    settings: MemorySettings | None = None,
    memory_space_id: str | None = None,
    palace_path: str | Path | None = None,
    locked: bool = True,
) -> EidolonDataMemoryEngine:
    """Build the ``eidolon_data`` memory port implementation.

    Tests and embedded runtimes may pass an already-constructed ``backend``.
    Production callers can pass ``settings`` plus either ``palace_path`` or a
    ``memory_space_id`` so this helper can construct the MemPalace backend.
    """

    resolved_backend = backend
    if resolved_backend is None:
        resolved_settings = settings or get_memory_settings()
        resolved_palace = _resolve_palace_path(
            resolved_settings,
            memory_space_id=memory_space_id,
            palace_path=palace_path,
        )
        resolved_backend = MemPalacePythonBackend(resolved_settings, str(resolved_palace))

    if locked and not isinstance(resolved_backend, LockedBackend):
        resolved_backend = LockedBackend(resolved_backend)
    return EidolonDataMemoryEngine(resolved_backend)


def open_eidolon_data_store(
    *,
    data_settings: DataSettings | None = None,
    backend: MemoryBackend | None = None,
    settings: MemorySettings | None = None,
    memory_space_id: str | None = None,
    palace_path: str | Path | None = None,
    locked: bool = True,
) -> DataStore:
    """Open ``DataStore`` with an eidolon_memory-backed memory engine."""

    engine = build_eidolon_data_memory_engine(
        backend=backend,
        settings=settings,
        memory_space_id=memory_space_id,
        palace_path=palace_path,
        locked=locked,
    )
    return DataStore.open(data_settings, memory_engine=engine)


def _resolve_palace_path(
    settings: MemorySettings,
    *,
    memory_space_id: str | None,
    palace_path: str | Path | None,
) -> Path:
    if palace_path is not None:
        return Path(palace_path).expanduser().resolve()
    if not memory_space_id:
        raise ValueError("memory_space_id or palace_path is required when backend is not supplied")
    return resolve_palace_for_memory_space(settings, memory_space_id)


__all__ = ["build_eidolon_data_memory_engine", "open_eidolon_data_store"]
