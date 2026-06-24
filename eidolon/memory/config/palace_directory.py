"""Resolve per-memory-space MemPalace palace directories on disk (D1).

Each memory space gets ``<palaces_root>/<memory_space_id>/``; the agent_runner
process owns exclusive PersistentClient access to that directory.

Resolution order for :func:`resolve_palace_for_user`:

1. ``path_override`` (caller-supplied absolute path)
2. ``settings.runtime.palaces_root`` joined with ``memory_space_id``

``palaces_root`` priority: ``EIDOLON_MEMORY_PALACES_ROOT`` env > config field
> ``~/eidolon/palaces`` fallback.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from eidolon.memory.config.memory_settings import MemorySettings

_MEMORY_SPACE_ID_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}"
    r"\.[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}"
    r"\.[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"
)


def validate_memory_space_id(memory_space_id: str) -> str:
    """Reject unsafe memory-space ids; prevents path injection."""
    mid = (memory_space_id or "").strip()
    if not _MEMORY_SPACE_ID_RE.fullmatch(mid):
        msg = (
            f"invalid memory_space_id {memory_space_id!r}; "
            "must match <tenant_id>.<owner_user_id>.<persona_id>"
        )
        raise ValueError(msg)
    return mid


def resolve_palaces_root(settings: MemorySettings) -> Path:
    """Env > config > ``~/eidolon/palaces`` default."""
    env = os.environ.get("EIDOLON_MEMORY_PALACES_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    configured = (settings.runtime.palaces_root or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "eidolon" / "palaces").resolve()


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
    return resolve_palaces_root(settings) / mid
