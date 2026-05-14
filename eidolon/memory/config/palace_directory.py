"""Resolve the MemPalace palace root directory on disk.

Callers should load :func:`get_memory_settings` first and pass the result here.
``runtime.palace_path`` in the settings YAML is the normal source of truth.

Resolution order for :func:`resolve_palace_directory`:

1. ``path_override`` (non-empty string or path), if the caller passes one
2. ``settings.runtime.palace_path`` when non-empty
3. ``~/eidolon/mempalace``
"""

from __future__ import annotations

from pathlib import Path

from eidolon.memory.config.memory_settings import MemorySettings


def resolve_palace_directory(
    settings: MemorySettings,
    *,
    path_override: str | Path | None = None,
) -> Path:
    """Return the palace directory, creating it if missing."""
    if path_override:
        p = Path(path_override).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    configured = (settings.runtime.palace_path or "").strip()
    if configured:
        p = Path(configured).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    p = (Path.home() / "eidolon" / "mempalace").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p
