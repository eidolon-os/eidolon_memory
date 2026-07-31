"""Resolve per-memory-space MemPalace palace directories on disk (D1).

Each memory space gets ``<palaces_root>/<storage_name>/``; the agent_runner
process owns exclusive PersistentClient access to that directory.

Resolution order for :func:`resolve_palace_for_user`:

1. ``path_override`` (caller-supplied absolute path)
2. ``settings.runtime.palaces_root`` joined with encoded ``memory_space_id``

``palaces_root`` priority: ``EIDOLON_MEMORY_PALACES_ROOT`` env > config field
> ``~/eidolon/memory/mempalaces`` fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

# Single source of truth for the memory_space_id grammar lives in the SDK; the
# path-injection guarantee here relies on that same validation. Re-exported so
# existing callers keep importing it from this module.
from eidolon_memory_contracts import memory_space_storage_name, validate_memory_space_id

from eidolon.memory.config.memory_settings import MemorySettings

__all__ = [
    "validate_memory_space_id",
    "resolve_palaces_root",
    "resolve_palace_for_memory_space",
    "memory_space_storage_name",
]


def resolve_palaces_root(settings: MemorySettings) -> Path:
    """Env > config > ``~/eidolon/memory/mempalaces`` default."""
    env = os.environ.get("EIDOLON_MEMORY_PALACES_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    configured = (settings.runtime.palaces_root or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "eidolon" / "memory" / "mempalaces").resolve()


def resolve_palace_for_memory_space(
    settings: MemorySettings,
    memory_space_id: str,
    *,
    path_override: str | Path | None = None,
) -> Path:
    """Return per-memory-space palace directory; does not create it."""
    if path_override:
        return Path(path_override).expanduser().resolve()
    mid = validate_memory_space_id(memory_space_id)
    return resolve_palaces_root(settings) / memory_space_storage_name(mid)
