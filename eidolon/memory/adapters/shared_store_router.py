"""Serve spaces whose storage lives on a server, from any replica.

Nothing here is tied to the host it runs on. No advisory lock, because there is no
local file to own. No port derived from a space id, because a replica serves every
space it is asked about. Two replicas are interchangeable, and that is the
property that makes horizontal scaling a deployment decision rather than a code
change — it is asserted by a test, not by this docstring.

A space still needs a palace directory, which deserves explanation. MemPalace
keeps a space's bookkeeping there: which embedder built it, which backend it is
bound to. With a vector server the memories live remotely, so that directory holds
only markers derived from configuration. A replica builds it in container-local
storage and discards it on exit — losing it loses nothing, and the next replica
rebuilds it identically. Pointing ``ephemeral_root`` at shared storage would
reintroduce exactly the coupling this router removes.

Isolation between spaces is not a directory here. Each space still gets its own
remote collection, named from its palace identity, so the boundary survives; what
does not survive is any claim of ownership over it.

What is not here yet: the knowledge graph and the ledgers. Both are SQLite files
in the local shape, and a replica keeping those on its own disk would stop being
interchangeable — so on shared storage they belong in a database, which is not
written. Until then this router serves vector recall with neither, which every
consumer already handles: the graph is optional by configuration, and each ledger
is independently optional.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.space_runtime import MemorySpaceRuntime, SpaceLedgers
from eidolon.memory.infrastructure.mempalace_backend import selected_mempalace_backend
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class SharedStoreRouter:
    """Stateless resolution against a remote vector store."""

    def __init__(
        self,
        settings: MemorySettings,
        *,
        ephemeral_root: Path,
    ) -> None:
        self._settings = settings
        self._ephemeral_root = Path(ephemeral_root)
        # Not ownership, unlike the local router's pool: nothing here is claimed
        # and no lock is held. It exists so repeated resolves within a replica's
        # lifetime skip re-materialising markers. Dropping it at any moment would
        # cost latency, not correctness.
        self._views: dict[str, MemorySpaceRuntime] = {}
        self._build_lock = asyncio.Lock()

    def serves(self, space_id: str) -> bool:
        """Any space, from any replica. That is what this router is for."""

        return bool(space_id.strip())

    async def resolve(self, space_id: str) -> MemorySpaceRuntime:
        existing = self._views.get(space_id)
        if existing is not None:
            return existing

        async with self._build_lock:
            existing = self._views.get(space_id)
            if existing is not None:
                return existing

            runtime = await asyncio.to_thread(self._build, space_id)
            self._views[space_id] = runtime
            log.info(
                "space_view_opened",
                memory_space_id=space_id,
                palace=runtime.palace_path,
            )
            return runtime

    def _build(self, space_id: str) -> MemorySpaceRuntime:
        """Materialise a space's bookkeeping and open the remote store."""

        palace_path = self._ephemeral_root / space_id
        palace_path.mkdir(parents=True, exist_ok=True)

        # Creates the remote collection if this is the space's first ever use, and
        # writes the local marker recording which server it is bound to. Both are
        # idempotent, so replicas racing on a new space converge.
        ensure_palace_initialized(
            space_id,
            palace_path,
            backend=selected_mempalace_backend(self._settings),
        )

        backend = LockedBackend(
            MemPalacePythonBackend(self._settings, str(palace_path), memory_space_id=space_id)
        )
        # The lock guards the shared ONNX embedding session rather than the store,
        # which handles its own concurrency. It can go once embedding is a service.
        #
        # The turn ring is deliberately absent: it is process memory, so under
        # several replicas a space's recent turns would depend on which replica
        # answered. Callers keep their own recent-turn window, and the field this
        # would have populated is not part of the read contract.
        backend.working_memory = WorkingMemoryRing(maxlen=0, lock=backend.lock)

        return MemorySpaceRuntime(
            space_id=space_id,
            backend=backend,
            palace_path=str(palace_path),
            kg=None,
            ledgers=SpaceLedgers(),
        )

    def held_spaces(self) -> list[str]:
        return sorted(self._views)

    async def aclose(self) -> None:
        """Nothing to release — which is the point."""

        self._views.clear()
