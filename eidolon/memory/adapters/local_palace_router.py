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
import time
from pathlib import Path
from typing import IO

from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon.memory.config.memory_settings import MemorySettings, resolve_run_dir
from eidolon.memory.config.palace_directory import resolve_palace_for_memory_space
from eidolon.memory.domain.space_runtime import (
    MemorySpaceRuntime,
    MemorySpaceUnavailable,
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
    run_integrity_check,
)
from eidolon.memory.infrastructure.mempalace_backend import (
    apply_mempalace_backend_env,
    mempalace_backend_env,
    reconcile_configured_backend,
    selected_mempalace_backend,
    vector_sqlite_integrity_targets,
)
from eidolon.memory.infrastructure.nats.names import nats_safe_name
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized
from eidolon.memory.infrastructure.sync_ledger import SyncLedger
from eidolon.memory.support import metrics
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class LocalPalaceRouter:
    """Pool of embedded-storage handles, one entry per space.

    Construction does no I/O. A space's handles are built the first time it is
    resolved, which keeps startup proportional to the number of spaces actually
    used rather than the number configured.

    It does prepare embedder resolution, because this class is the only source of
    store handles — a rule the layering suite enforces — so it is the one place
    "before any store is opened" can be guaranteed. Entrypoints also apply it
    while setting up the MemPalace environment; the call is idempotent, and
    having both means a caller that assembles a router directly cannot end up
    embedding with MemPalace's default while the palace records something else.

    That call writes to ``os.environ``, so it is process-global and not scoped to
    this router. That is not a leak to be tidied up: MemPalace takes its backend
    and embedder from the environment, so a process has exactly one of each no
    matter how many routers it holds. Two routers with different storage settings
    in one process would already be incoherent, and the second would win.
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
        apply_mempalace_backend_env(settings)
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
        # Resolved palace directory → the space that holds it. The flock above
        # answers "is another process serving this space"; this answers "is another
        # space in this process already using this directory", which is a different
        # question with the same consequence. See ``_claim_palace_directory``.
        self._palace_dirs: dict[str, str] = {}
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
                raise MemorySpaceUnavailable(
                    f"refusing to open memory space {space_id!r}: this process already "
                    f"holds {len(self._runtimes)} of at most {self._max_spaces}"
                )

            opening = time.perf_counter()
            runtime = await asyncio.to_thread(self._build, space_id)
            self._runtimes[space_id] = runtime
            metrics.SPACE_OPEN_SECONDS.observe(time.perf_counter() - opening)
            # How well the resident embedding model is amortised: one space per
            # process means paying for a model per space, and this is the number
            # that says whether that is still the case.
            metrics.SPACES_HELD.set(len(self._runtimes))
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
            # Shares the vector store's lock: a turn writes to both, and one
            # critical section over the pair beats an ordering between two.
            kg = SqliteKnowledgeGraph(
                palace_path / "knowledge_graph.sqlite3",
                space_id=space_id,
                lock=backend.lock,
            )

        return MemorySpaceRuntime(
            space_id=space_id,
            backend=backend,
            palace_path=str(palace_path),
            kg=kg,
            ledgers=SpaceLedgers(
                command_status=CommandStatusLedger(
                    palace_path / "command_status.sqlite3",
                    space_id=space_id,
                    retention_days=self._settings.command_status.retention_days,
                    max_records=self._settings.command_status.max_records,
                    prune_every_writes=self._settings.command_status.prune_every_writes,
                ),
                dlq=DlqLedger(palace_path / "dlq.sqlite3", space_id=space_id),
                decisions=ExtractionDecisionLedger(
                    palace_path / "extraction_decisions.sqlite3"
                ),
                canonical_facts=CanonicalFactLedger(palace_path / "canonical_facts.sqlite3"),
                commitments=CommitmentLedger(palace_path / "commitments.sqlite3"),
                sync=SyncLedger(palace_path / "sync_ledger.sqlite3", space_id=space_id),
            ),
        )

    def _claim_palace_directory(self, space_id: str, palace_path: Path) -> None:
        """Refuse a second space that resolves to a directory this one already holds.

        The on-disk flock is keyed on the *space id*, which is the right key for
        the cross-process question — two processes must not serve one space. It is
        the wrong key for this one: the resource Chroma cannot share is the
        **directory**, and two different space ids can name the same directory.

        ``palace_path_override`` does exactly that. It is applied to every space
        this router resolves, so a process holding two spaces with an override in
        effect would compute one path twice, take two differently-named flocks
        because the names come from the space ids, and open the same
        ``chroma.sqlite3`` twice — the corruption the claim exists to prevent,
        arriving through the mechanism meant to prevent it.

        Unreachable while a process serves a single space, which is why it has not
        bitten. It becomes reachable the moment one process holds several, so it is
        closed here rather than left as a note. In-process only, because the
        cross-process case is already covered and because renaming the on-disk lock
        would leave an upgraded process holding a path the running one does not
        recognise.
        """

        resolved = str(palace_path.expanduser().resolve())
        holder = self._palace_dirs.get(resolved)
        if holder is not None and holder != space_id:
            raise MemorySpaceUnavailable(
                f"refusing to open memory space {space_id!r}: this process already "
                f"serves {holder!r} from {resolved}. A palace directory has exactly "
                f"one owner, and two spaces resolving to one directory is normally "
                f"a palace_path_override applied to more than one space."
            )
        self._palace_dirs[resolved] = space_id

    def _prepare_palace(self, space_id: str, palace_path: Path) -> None:
        """Make a palace directory safe to open, or refuse to open it.

        Ordering matters. The lock comes first so nothing below races another
        process, and the directory claim comes with it so nothing races another
        space in *this* process. The location guard comes before any write, because
        a palace on a synced or networked filesystem corrupts rather than failing
        cleanly. Integrity runs last, once the databases exist, and a failure here
        stops this space — not the whole process, which may be serving others fine.
        """

        self._acquire_space_lock(space_id)
        self._claim_palace_directory(space_id, palace_path)
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
            if kg_path.is_file():
                # Only check what already exists. The graph creates its schema on
                # open, so a first run has nothing here yet — and an absent file
                # is not a corrupt one.
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
        # The filename must stay as it is. A running process holds this exact
        # path, so renaming it — however much better "space" reads than "agent"
        # now that a process is not one space — would make an upgraded process
        # take no lock the old one recognises. Both would then open the same
        # palace, which is the corruption this claim exists to prevent, and the
        # window is any deployment that is not a clean full stop.
        lock_path = run_dir / f"eidolon-memory-agent-{nats_safe_name(space_id)}.lock"
        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip()
            handle.close()
            # The wording is load-bearing: operators grep for it, and so does
            # tests/memory/e2e/test_concurrency_topology.py. Keep it stable even
            # though "process" would now read better than "eidolon-memory-agent".
            raise MemorySpaceUnavailable(
                f"memory_space_id {space_id!r} is already owned by another "
                f"eidolon-memory-agent; lock={lock_path} holder={holder!r}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._locks[space_id] = handle

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
        metrics.SPACES_HELD.set(0)
        self._palace_dirs.clear()
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
