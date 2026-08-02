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

Four of the six ledgers live in that same database; the remaining two
(commitments, canonical facts) are not implemented for it yet and are simply
absent, which every consumer already handles.

One connection pool serves the whole replica — the graph and every ledger of
every space. Connections then scale with concurrency rather than with how many
tenants this replica has been asked about, which is the only version that
survives more than a handful of spaces.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from eidolon.memory.adapters.kg_postgres import PostgresKnowledgeGraph
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.space_runtime import MemorySpaceRuntime, SpaceLedgers
from eidolon.memory.infrastructure.ledgers_postgres import (
    PostgresCommandStatusLedger,
    PostgresCommitmentLedger,
    PostgresDlqLedger,
    PostgresExtractionDecisionLedger,
    PostgresSyncLedger,
    require_pool_driver,
)
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
        # One pool for the whole replica, not one per space and not one per
        # ledger. Connections then scale with concurrency, which is what a pool
        # is for, instead of with the number of tenants this replica has been
        # asked about. Per-space pools reach a server's connection limit after a
        # handful of spaces — five pools of eight would exhaust a default
        # PostgreSQL at the third one.
        self._pool: Any = None
        self._pool_dsn: str = ""

    def serves(self, space_id: str) -> bool:
        """Any space, from any replica. That is what this router is for."""

        return bool(space_id.strip())

    async def _shared_pool(self, dsn: str) -> Any:
        """The replica's one connection pool, opened on first use.

        Sized for concurrent requests rather than for spaces. Callers pass their
        space id on every statement, so one pool serves all of them.
        """

        if self._pool is not None:
            if dsn != self._pool_dsn:
                # Both the graph and the ledgers read their own setting, so a
                # deployment can point them at different servers. Sharing one
                # pool between two DSNs would silently send half the statements
                # to the wrong database.
                raise ValueError(
                    "kg and ledgers must use the same database on shared storage; "
                    "got two different connection strings"
                )
            return self._pool

        pool_cls = require_pool_driver()
        # min_size 1 so an idle replica holds almost nothing; max_size is the
        # concurrency ceiling for this replica against the database.
        self._pool = pool_cls(dsn, min_size=1, max_size=16, open=False)
        await self._pool.open()
        self._pool_dsn = dsn
        return self._pool

    async def resolve(self, space_id: str) -> MemorySpaceRuntime:
        existing = self._views.get(space_id)
        if existing is not None:
            return existing

        async with self._build_lock:
            existing = self._views.get(space_id)
            if existing is not None:
                return existing

            runtime = await asyncio.to_thread(self._build, space_id)
            # The graph and the ledgers need their own connections, which are
            # async to open, so they are attached after the storage handles
            # rather than inside _build.
            graph = await self._open_graph(space_id)
            if graph is not None:
                runtime = replace(runtime, kg=graph)
            runtime = replace(runtime, ledgers=await self._open_ledgers(space_id))
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

    async def _open_ledgers(self, space_id: str) -> SpaceLedgers:
        """The space's records in the shared database, or none of them.

        ``ledgers.backend='palace'`` on shared storage is not a usable
        combination — the files would be per-replica, so a fact invalidated by
        one replica would still be recalled by another. It is served without them
        rather than refused, because vector recall alone is a working service and
        a hard failure at startup would take down a deployment over a setting
        that can be corrected while it runs.
        """

        if self._settings.ledgers.backend != "postgres":
            log.warning(
                "space_ledgers_backend_unsupported",
                memory_space_id=space_id,
                backend=self._settings.ledgers.backend,
                detail="shared storage needs ledgers.backend=postgres; serving without them",
            )
            return SpaceLedgers()

        dsn = self._settings.ledgers.resolve_postgres_dsn()
        if not dsn:
            log.warning(
                "space_ledgers_dsn_missing",
                memory_space_id=space_id,
                env=self._settings.ledgers.postgres_dsn_env,
                detail="serving without ledgers",
            )
            return SpaceLedgers()

        pool = await self._shared_pool(dsn)
        ledgers = SpaceLedgers(
            decisions=PostgresExtractionDecisionLedger(pool),
            sync=PostgresSyncLedger(pool, space_id=space_id),
            dlq=PostgresDlqLedger(pool, space_id=space_id),
            command_status=PostgresCommandStatusLedger(pool, space_id=space_id),
            commitments=PostgresCommitmentLedger(pool),
        )
        # Idempotent, and cheap after the first space: CREATE TABLE IF NOT EXISTS
        # on tables another replica may be creating at the same moment.
        for ledger in (
            ledgers.decisions,
            ledgers.sync,
            ledgers.dlq,
            ledgers.command_status,
            ledgers.commitments,
        ):
            await ledger.ensure_schema()
        return ledgers

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

        graph = PostgresKnowledgeGraph(await self._shared_pool(dsn), space_id=space_id)
        await graph.ensure_schema()
        return graph

    def held_spaces(self) -> list[str]:
        return sorted(self._views)

    async def aclose(self) -> None:
        """Close the replica's pool. No claims to hand back — which is the point.

        One pool to close, because the graph and every ledger of every space
        share it. Closing per handle would have to know how many handles hold the
        same pool, and closing none of them leaks the connections.
        """

        self._views = {}
        pool, self._pool = self._pool, None
        self._pool_dsn = ""
        if pool is None:
            return
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
            log.warning("space_pool_close_failed", error=str(exc))
