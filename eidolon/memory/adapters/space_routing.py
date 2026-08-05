"""Build the router that resolves a space to its storage handles.

Everything above this function takes a ``MemorySpaceRouter`` and cannot tell
which implementation it got. That indirection is the point and it stays: the
abstraction is what let one process serve many spaces at all, and it is what a
second storage shape would plug into.

Today there is one implementation, because this deployment is local: vectors in a
Chroma file and everything else in SQLite, inside each palace directory. Embedded
storage has a hard consequence — one owning process per palace — which is why the
router, not the caller, decides how a space is opened.

``FixedSpaceRouter`` also exists, for the case where the handles were opened
elsewhere. It is not an alternative deployment shape; it is a wrapper.
"""

from __future__ import annotations

from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.space_runtime import MemorySpaceRouter


def build_space_router(
    settings: MemorySettings,
    *,
    allowed_spaces: list[str] | None = None,
    palace_path_override: str | None = None,
) -> MemorySpaceRouter:
    """Build the router for this deployment.

    ``allowed_spaces`` shards spaces across processes, bounding what one crash
    takes down. ``None`` means this process answers for any space it is asked
    about.
    """

    return LocalPalaceRouter(
        settings,
        allowed_spaces=allowed_spaces,
        palace_path_override=palace_path_override,
    )
