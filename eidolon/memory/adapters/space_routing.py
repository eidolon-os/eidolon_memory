"""Pick the router this deployment's storage implies.

There is no cloud switch. Which router runs follows from where storage lives,
because that is the only thing the choice actually depends on: embedded storage
must have one owning process per palace, and remote storage must not pretend to.
Adding a separate `mode: local | cloud` setting would let the two disagree — a
deployment could then claim to be cloud while keeping its data in a file — so the
storage config is the single source of truth.

Everything above this function takes a router and cannot tell which it got.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter
from eidolon.memory.adapters.shared_store_router import SharedStoreRouter
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.space_runtime import MemorySpaceRouter
from eidolon.memory.infrastructure.mempalace_backend import selected_mempalace_backend

#: Vector backends whose data lives in the palace directory. A palace on one of
#: these has to be owned by exactly one process; anything else is served by
#: replicas that own nothing.
_EMBEDDED_VECTOR_BACKENDS = frozenset({"chroma"})


def storage_is_embedded(settings: MemorySettings) -> bool:
    """Whether this deployment's memories live on local disk."""

    return selected_mempalace_backend(settings) in _EMBEDDED_VECTOR_BACKENDS


def build_space_router(
    settings: MemorySettings,
    *,
    allowed_spaces: list[str] | None = None,
    palace_path_override: str | None = None,
    ephemeral_root: Path | None = None,
) -> MemorySpaceRouter:
    """Build the router for this deployment.

    ``allowed_spaces`` shards embedded storage across processes, bounding what one
    crash takes down. It is meaningless on shared storage, where every replica
    serves everything — passing it there would be describing an affinity the
    architecture does not have, so it is ignored rather than half-honoured.
    """

    if storage_is_embedded(settings):
        return LocalPalaceRouter(
            settings,
            allowed_spaces=allowed_spaces,
            palace_path_override=palace_path_override,
        )

    return SharedStoreRouter(
        settings,
        # Container-local by default. Only MemPalace's per-space markers land
        # here; putting it on a shared volume would recreate the coupling the
        # shared-store router exists to remove.
        ephemeral_root=ephemeral_root
        or Path(tempfile.gettempdir()) / "eidolon-memory-spaces",
    )
