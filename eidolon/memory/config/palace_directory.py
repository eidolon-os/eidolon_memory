"""Resolve per-user MemPalace palace directories on disk (D1).

Each user gets ``<palaces_root>/<user_id>/``; the agent_runner process owns
exclusive PersistentClient access to that directory. Cross-user palaces are
physically isolated.

Resolution order for :func:`resolve_palace_for_user`:

1. ``path_override`` (caller-supplied absolute path)
2. ``settings.runtime.palaces_root`` joined with ``user_id``

``palaces_root`` priority: ``EIDOLON_MEMORY_PALACES_ROOT`` env > config field
> ``~/eidolon/palaces`` fallback.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from eidolon.memory.config.memory_settings import MemorySettings

_USER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def validate_user_id(user_id: str) -> str:
    """Reject ids with separators / control chars; prevents path injection."""
    uid = (user_id or "").strip()
    if not _USER_ID_RE.fullmatch(uid):
        msg = f"invalid user_id {user_id!r}; must match {_USER_ID_RE.pattern}"
        raise ValueError(msg)
    return uid


def resolve_palaces_root(settings: MemorySettings) -> Path:
    """Env > config > ``~/eidolon/palaces`` default."""
    env = os.environ.get("EIDOLON_MEMORY_PALACES_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    configured = (settings.runtime.palaces_root or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "eidolon" / "palaces").resolve()


def resolve_palace_for_user(
    settings: MemorySettings,
    user_id: str,
    *,
    path_override: str | Path | None = None,
) -> Path:
    """Return per-user palace directory; **does not create it** (caller / ensure_palace_initialized)."""
    if path_override:
        return Path(path_override).expanduser().resolve()
    uid = validate_user_id(user_id)
    return resolve_palaces_root(settings) / uid
