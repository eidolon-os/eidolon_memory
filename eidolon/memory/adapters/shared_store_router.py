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

The graph lives in the shared database when one is configured, and is simply
absent otherwise — the same runtime choice `kg.backend` expresses locally.

The ledgers are still missing. They are SQLite files in the local shape, and a
replica keeping those on its own disk would stop being interchangeable, so on
shared storage they belong in the database too. Until they are written this router
serves without them, which every consumer already handles: each ledger is
independently optional.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from eidolon.memory.adapters.kg_postgres import PostgresKnowledgeGraph
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
            # The graph needs its own connection, which is async to open, so it
            # is attached after the storage handles rather than inside _build.
            graph = await self._open_graph(space_id)
            if graph is not None:
                runtime = replace(runtime, kg=graph)
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

    async def _open_graph(self, space_id: str):
        """Connect this space's graph, or None when the deployment has none.

        A missing connection string is a configuration answer, not a failure:
        `kg.backend` says whether a graph is wanted, and the service runs on
        vector recall alone when it is not.
        """

        if not self._settings.kg.enabled:
            return None
        if self._settings.kg.backend != "postgres":
            # A file-backed graph on a replica would be per-replica state, which
            # is the thing this router exists not to have.
            log.warning(
                "space_graph_backend_unsupported",
                memory_space_id=space_id,
                backend=self._settings.kg.backend,
                detail="shared storage needs kg.backend=postgres; serving without a graph",
            )
            return None

        dsn = self._settings.kg.resolve_postgres_dsn()
        if not dsn:
            log.warning(
                "space_graph_dsn_missing",
                memory_space_id=space_id,
                env=self._settings.kg.postgres_dsn_env,
                detail="serving without a graph",
            )
            return None

        # Importing this does not pull in psycopg — that happens inside connect(),
        # so a deployment without the extra can still load this module.
        return await PostgresKnowledgeGraph.connect(dsn, space_id=space_id)

    def held_spaces(self) -> list[str]:
        return sorted(self._views)

    async def aclose(self) -> None:
        """Release connection pools. No claims to hand back — which is the point."""

        views, self._views = self._views, {}
        for space_id, runtime in views.items():
            closer = getattr(runtime.kg, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                log.warning(
                    "space_graph_close_failed",
                    memory_space_id=space_id,
                    error=str(exc),
                )
