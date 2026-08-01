"""Serve spaces whose storage is embedded in a directory on this host.

One process, many spaces. Each space gets its own palace directory, its own
storage handles and its own lock, so the single-owner guarantee that embedded
storage needs is unchanged — a palace still has exactly one process holding it.
What changes is that the process is no longer *defined* by one space.

The reason this matters is the embedding model. It is a few hundred megabytes
resident, it is per-process rather than per-space, and MemPalace already caches it
by (model, providers) — so the second space a process opens costs a few megabytes
rather than another whole model. Measured across three palaces in one process:
291MB, then +10MB, then +5MB. That ratio is what decides how many spaces a host
can serve.

Handles are opened on first use and kept. There is no eviction: a space that has
been resolved once is one this deployment serves, and its handles are small next
to the model they share. What bounds the pool is ``max_spaces``, which refuses a
new space rather than quietly opening an unbounded number of SQLite files.

Opening a space is more than constructing handles — the palace has to exist, the
configured backend has to match what is on disk, the databases have to pass an
integrity check, and this host has to be the only one holding the directory. All
of that lives here rather than in a process entrypoint, because every one of
those steps is per-space, and a caller that had to remember them would eventually
forget one.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
from typing import IO

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon.memory.config.memory_settings import MemorySettings, resolve_run_dir
from eidolon.memory.config.palace_directory import resolve_palace_for_memory_space
from eidolon.memory.domain.space_runtime import (
    MemorySpaceRuntime,
    SpaceLedgers,
    UnknownMemorySpace,
)
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.command_status import CommandStatusLedger
from eidolon.memory.infrastructure.commitments import CommitmentLedger
from eidolon.memory.infrastructure.dlq import DlqLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger
from eidolon.memory.infrastructure.integrity import (
    IntegrityCheckFailed,
    assert_palace_location_safe,
    fsync_directory,
    run_integrity_check,
)
from eidolon.memory.infrastructure.mempalace_backend import (
    mempalace_backend_env,
    reconcile_configured_backend,
    selected_mempalace_backend,
    vector_sqlite_integrity_targets,
)
from eidolon.memory.infrastructure.nats.names import nats_safe_name
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized
from eidolon.memory.infrastructure.sync_ledger import SyncLedger
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class LocalPalaceRouter:
    """Pool of embedded-storage handles, one entry per space.

    Construction does no I/O. A space's handles are built the first time it is
    resolved, which keeps startup proportional to the number of spaces actually
    used rather than the number configured.
    """

    def __init__(
        self,
        settings: MemorySettings,
        *,
        allowed_spaces: list[str] | None = None,
        max_spaces: int = 64,
        palace_path_override: str | None = None,
    ) -> None:
        self._settings = settings
        # None means "any space this deployment is asked about". A list restricts
        # to a shard, which is how a supervisor splits spaces across processes to
        # bound the blast radius of one crashing.
        self._allowed = set(allowed_spaces) if allowed_spaces else None
        self._max_spaces = max_spaces
        self._palace_path_override = palace_path_override
        self._runtimes: dict[str, MemorySpaceRuntime] = {}
        # One advisory lock per space, not one per process. Holding several is
        # what lets a process serve several spaces while each palace still has
        # exactly one owner.
        self._locks: dict[str, IO] = {}
        # Guards the pool itself, not the spaces in it. Held only while building
        # an entry, so resolving different spaces does not serialise, and a space
        # under construction is built once rather than twice.
        self._pool_lock = asyncio.Lock()

    def serves(self, space_id: str) -> bool:
        if self._allowed is None:
            return True
        return space_id in self._allowed

    async def resolve(self, space_id: str) -> MemorySpaceRuntime:
        existing = self._runtimes.get(space_id)
        if existing is not None:
            return existing

        if not self.serves(space_id):
            raise UnknownMemorySpace(
                f"this deployment does not serve memory space {space_id!r}"
            )

        async with self._pool_lock:
            # Re-check: another caller may have built it while we waited.
            existing = self._runtimes.get(space_id)
            if existing is not None:
                return existing

            if len(self._runtimes) >= self._max_spaces:
                raise UnknownMemorySpace(
                    f"refusing to open memory space {space_id!r}: this process already "
                    f"holds {len(self._runtimes)} of at most {self._max_spaces}"
                )

            runtime = await asyncio.to_thread(self._build, space_id)
            self._runtimes[space_id] = runtime
            log.info(
                "space_runtime_opened",
                memory_space_id=space_id,
                palace=runtime.palace_path,
                kg=runtime.has_kg,
                held=len(self._runtimes),
            )
            return runtime

    def _build(self, space_id: str) -> MemorySpaceRuntime:
        """Open one space's storage. Runs off the event loop — it touches disk."""

        palace_path = (
            Path(self._palace_path_override)
            if self._palace_path_override
            else resolve_palace_for_memory_space(self._settings, space_id)
        )
        self._prepare_palace(space_id, palace_path)

        backend = LockedBackend(
            MemPalacePythonBackend(self._settings, str(palace_path), memory_space_id=space_id)
        )
        # The ring shares the backend's lock deliberately — one lock per space to
        # reason about, and no ordering between two of them to get wrong.
        backend.working_memory = WorkingMemoryRing(
            maxlen=self._settings.runtime.working_memory_maxlen,
            lock=backend.lock,
        )

        kg = None
        if self._settings.kg.enabled:
            # MemPalace is a third-party import, deferred so a deployment with the
            # graph off does not pay for loading it.
            from mempalace.knowledge_graph import KnowledgeGraph

            kg = LockedKnowledgeGraph(
                KnowledgeGraph(db_path=str(palace_path / "knowledge_graph.sqlite3")),
                backend.lock,
            )

        return MemorySpaceRuntime(
            space_id=space_id,
            backend=backend,
            palace_path=str(palace_path),
            kg=kg,
            ledgers=SpaceLedgers(
                command_status=CommandStatusLedger(
                    palace_path / "command_status.sqlite3",
                    retention_days=self._settings.command_status.retention_days,
                    max_records=self._settings.command_status.max_records,
                    prune_every_writes=self._settings.command_status.prune_every_writes,
                ),
                dlq=DlqLedger(palace_path / "dlq.sqlite3"),
                decisions=ExtractionDecisionLedger(
                    palace_path / "extraction_decisions.sqlite3"
                ),
                canonical_facts=CanonicalFactLedger(palace_path / "canonical_facts.sqlite3"),
                commitments=CommitmentLedger(palace_path / "commitments.sqlite3"),
                sync=SyncLedger(palace_path / "sync_ledger.sqlite3"),
            ),
        )

    def _prepare_palace(self, space_id: str, palace_path: Path) -> None:
        """Make a palace directory safe to open, or refuse to open it.

        Ordering matters. The lock comes first so nothing below races another
        process. The location guard comes before any write, because a palace on
        a synced or networked filesystem corrupts rather than failing cleanly.
        Integrity runs last, once the databases exist, and a failure here stops
        this space — not the whole process, which may be serving others fine.
        """

        self._acquire_space_lock(space_id)
        assert_palace_location_safe(palace_path)

        backend_name = selected_mempalace_backend(self._settings)
        report = reconcile_configured_backend(palace_path, backend_name)
        if report.removed_artifacts:
            log.warning(
                "space_removed_empty_backend_artifacts",
                memory_space_id=space_id,
                configured_backend=backend_name,
                removed=list(report.removed_artifacts),
            )

        ensure_palace_initialized(
            space_id,
            palace_path,
            backend=backend_name,
            env=mempalace_backend_env(self._settings),
        )

        targets = list(vector_sqlite_integrity_targets(palace_path, backend_name))
        if self._settings.kg.enabled:
            kg_path = palace_path / "knowledge_graph.sqlite3"
            self._materialize_kg_file(kg_path)
            targets.append(("kg", kg_path))

        for label, db_path in targets:
            result = run_integrity_check(str(db_path), quick=False)
            if not result.ok:
                raise IntegrityCheckFailed(
                    f"refusing to open memory space {space_id!r}: {label} "
                    f"integrity_check failed ({result.detail!r}); "
                    "investigate and restore from a snapshot"
                )

    def _acquire_space_lock(self, space_id: str) -> None:
        """Take this host's exclusive claim on one space.

        Guards two things at once: the palace directory's single-owner rule, and
        ownership of the space's durable JetStream consumers. Two processes
        serving one space would route its writes unpredictably, so this fails
        fast rather than degrading.
        """

        if space_id in self._locks:
            return

        run_dir = resolve_run_dir(self._settings)
        run_dir.mkdir(parents=True, exist_ok=True)
        lock_path = run_dir / f"eidolon-memory-space-{nats_safe_name(space_id)}.lock"
        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip()
            handle.close()
            raise UnknownMemorySpace(
                f"memory space {space_id!r} is already held by another process; "
                f"lock={lock_path} holder={holder!r}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._locks[space_id] = handle

    @staticmethod
    def _materialize_kg_file(kg_sqlite_path: Path) -> None:
        """Create the graph database so the integrity check has a committed file."""

        from mempalace.knowledge_graph import KnowledgeGraph

        kg_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        graph = KnowledgeGraph(db_path=str(kg_sqlite_path))
        try:
            graph.close()
        except Exception as exc:  # noqa: BLE001 - the file is what we needed
            log.warning("space_kg_materialize_close_failed", error=str(exc))
        fsync_directory(kg_sqlite_path.parent)

    def held_spaces(self) -> list[str]:
        """Spaces whose handles are currently open. For diagnostics."""

        return sorted(self._runtimes)

    async def aclose(self) -> None:
        runtimes, self._runtimes = self._runtimes, {}
        for space_id, runtime in runtimes.items():
            if runtime.kg is not None:
                try:
                    runtime.kg.close()
                except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                    log.warning(
                        "space_runtime_kg_close_failed",
                        memory_space_id=space_id,
                        error=str(exc),
                    )

        # Release the claims last, so nothing else can take a space while we are
        # still closing its databases.
        locks, self._locks = self._locks, {}
        for space_id, handle in locks.items():
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                log.warning(
                    "space_lock_release_failed",
                    memory_space_id=space_id,
                    error=str(exc),
                )
