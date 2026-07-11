"""User CRUD orchestration with cascade-delete compensation.

Sits between the HTTP layer (``entrypoints/admin_api.py``) and the
process layer (``entrypoints/supervisor.py``). Knows:

  * how to read admin's user registry through the same loader supervisor uses
  * how to drive the supervisor through a state transition without
    racing its own reconcile loop (serialized via an asyncio.Lock)
  * how to clean up memory-owned palace data after admin removes a user

The cascade for DELETE is documented inline in :func:`delete_user`.

Why a separate module (not put it in supervisor.py):
    Supervisor is the *runtime* — it manages live processes. This module
    is the *control plane* — it composes supervisor operations into
    higher-level transactions. Keeping them separate means the control
    plane can be unit-tested with a stub supervisor protocol, while the
    real supervisor stays focused on subprocess management.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from eidolon.memory.config.users import (
    ConsolidatorUserConfig,
    UserEntry,
    load_users_config,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


# ---- public errors ----------------------------------------------------------


class UserAdminError(Exception):
    """Base for control-plane failures; the HTTP layer maps to status codes."""

    status_code: int = 500


class UserAlreadyExists(UserAdminError):
    status_code = 409


class UserNotFound(UserAdminError):
    status_code = 404


class PortConflict(UserAdminError):
    status_code = 409


class WorkerNotTerminated(UserAdminError):
    """Worker is still alive after a terminate was requested.

    Recoverable: caller should retry DELETE after a short delay, or
    investigate why the worker is stuck.
    """

    status_code = 503


class PalaceCleanupFailed(UserAdminError):
    """File-system error while moving the palace to trash."""

    status_code = 503


class UserRegistryReadOnly(UserAdminError):
    status_code = 409


class UserStillRegistered(UserAdminError):
    status_code = 409


class RebuildAlreadyRunning(UserAdminError):
    status_code = 409


class RebuildJobNotFound(UserAdminError):
    status_code = 404


# ---- protocols (so tests can stub) ------------------------------------------


@runtime_checkable
class _SupervisorProtocol(Protocol):
    """The slice of Supervisor that user_admin needs.

    Defined as a protocol so the orchestration tests can substitute a
    minimal fake; the real Supervisor in ``entrypoints/supervisor.py``
    implements all of these.
    """

    async def reconcile_now(self) -> None:
        """Re-read admin's registry and align running children. Must be safe to
        call concurrently with the supervisor's internal reconcile loop —
        callers are expected to serialize via the admin lock above.
        """
        ...

    def is_worker_alive(self, user_id: str) -> bool:
        """True if an agent_runner child for this user is currently running."""
        ...

    def palace_path_for(self, user: UserEntry) -> Path:
        """Where would this user's palace live on disk."""
        ...

    def palace_initialized(self, user: UserEntry) -> bool:
        """True when the selected MemPalace backend artifact is ready."""
        ...

    async def rebuild_memory_index(self, user: UserEntry, *, log_path: Path) -> dict:
        """Stop this user's runtime, rebuild its MemPalace vector index, then
        reconcile the runtime back to the registry's desired state.
        """
        ...


# ---- port allocation --------------------------------------------------------


# Auto-allocate range for legacy memory-local user creation. Admin-owned
# memory realms now use the SDK stable route contract, but keeping this range
# out of the fixed 8xxx service ports avoids collisions when old tooling calls
# into this helper.
_AUTO_PORT_MIN = 10030
_AUTO_PORT_MAX = 10100


def allocate_port(existing: Iterable[UserEntry]) -> int:
    """Return the lowest unused port in [_AUTO_PORT_MIN, _AUTO_PORT_MAX).

    Raises :class:`PortConflict` if the range is full. Only considers
    *configured* ports (regardless of enabled flag) — we don't want a
    new user to silently reclaim a disabled user's port.
    """
    taken = {u.port for u in existing}
    for port in range(_AUTO_PORT_MIN, _AUTO_PORT_MAX):
        if port not in taken:
            return port
    raise PortConflict(
        f"no free port in [{_AUTO_PORT_MIN}, {_AUTO_PORT_MAX}); "
        "remove unused users or specify a port explicitly"
    )


# ---- view models (returned by list/get) -------------------------------------


def user_to_view(
    user: UserEntry, *, worker_alive: bool, palace_path: Path, palace_initialized: bool
) -> dict:
    """The flat JSON shape the HTTP layer returns.

    ``mcp_http_url`` is the realm worker's MCP endpoint.
    Memory is authoritative for port assignment, so we expose the URL
    here rather than make admin synthesize from convention.
    """
    return {
        "spec": {
            "memory_realm_id": user.id,
            "memory_space_id": user.id,
            "owner_id": user.owner_id,
            "companion_id": user.companion_id,
            "display_name": user.id,  # memory has no display name field today
            "enabled": user.enabled,
            "palace_path": str(palace_path),
            "consolidator": {
                "enabled": user.consolidator.enabled if user.consolidator else False,
                "interval_hours": user.consolidator.interval_hours if user.consolidator else 6.0,
                "window_days": user.consolidator.window_days if user.consolidator else 30,
                "min_drawers": user.consolidator.min_drawers if user.consolidator else 3,
                "min_confidence": user.consolidator.min_confidence if user.consolidator else 0.6,
            },
            "created_at": datetime.now(UTC).isoformat(),
        },
        "health": {
            "worker_running": worker_alive and user.enabled,
            "mcp_reachable": worker_alive and user.enabled,  # liveness conflates the two for now
            "palace_initialized": palace_initialized,
            "note": "" if user.enabled else "memory realm disabled by admin registry",
        },
        "companion_ids": [user.companion_id] if user.companion_id else [],
        "mcp_http_url": f"http://127.0.0.1:{user.port}/mcp",
    }


@dataclass
class RebuildIndexJob:
    job_id: str
    memory_realm_id: str
    status: str
    log_path: Path
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: dict | None = None

    def to_view(self) -> dict:
        return {
            "job_id": self.job_id,
            "memory_realm_id": self.memory_realm_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "log_path": str(self.log_path),
            "error": self.error,
            "result": self.result,
        }


# ---- orchestration ----------------------------------------------------------


class UserAdmin:
    """Control plane for memory's user lifecycle.

    Holds an internal lock so concurrent HTTP calls don't race the
    supervisor's reconcile state. All public methods are async and
    must be awaited inside the supervisor's asyncio loop.
    """

    def __init__(
        self,
        supervisor: _SupervisorProtocol,
        *,
        trash_root: Path | None = None,
        maintenance_log_root: Path | None = None,
        user_log_root: Path | None = None,
    ) -> None:
        self._sup = supervisor
        self._lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._rebuild_jobs: dict[str, RebuildIndexJob] = {}
        self._rebuild_tasks: dict[str, asyncio.Task] = {}
        # Where ``delete_user`` moves a palace before final yaml removal.
        # Putting trash in a sibling of the palaces root keeps it on the same
        # filesystem (so ``shutil.move`` stays atomic via ``os.rename``).
        if trash_root is None:
            # Default: ~/.eidolon-trash — discoverable, not under ~/eidolon/
            # so a wipe-all-palaces command never accidentally erases trash.
            trash_root = Path.home() / ".eidolon-trash"
        self._trash_root = trash_root
        if maintenance_log_root is None:
            maintenance_log_root = Path.home() / "eidolon" / "logs" / "memory" / "maintenance"
        self._maintenance_log_root = maintenance_log_root
        if user_log_root is None:
            user_log_root = Path.home() / "eidolon" / "logs" / "memory"
        self._user_log_root = user_log_root

    # -------------------- list / get --------------------

    def list_users(self) -> list[dict]:
        config = load_users_config()
        return [
            user_to_view(
                u,
                worker_alive=self._sup.is_worker_alive(u.id),
                palace_path=self._sup.palace_path_for(u),
                palace_initialized=self._sup.palace_initialized(u),
            )
            for u in config.users
        ]

    def get_user(self, user_id: str) -> dict:
        config = load_users_config()
        user = config.find(user_id)
        if user is None:
            raise UserNotFound(f"user {user_id!r} not found")
        return user_to_view(
            user,
            worker_alive=self._sup.is_worker_alive(user.id),
            palace_path=self._sup.palace_path_for(user),
            palace_initialized=self._sup.palace_initialized(user),
        )

    async def reconcile(self) -> None:
        async with self._lock:
            await self._sup.reconcile_now()

    # -------------------- memory index rebuild --------------------

    async def start_rebuild_index(self, memory_realm_id: str) -> dict:
        """Create an async job that rebuilds one memory realm's MemPalace vector index."""
        async with self._lock:
            config = load_users_config()
            entry = config.find(memory_realm_id)
            if entry is None:
                raise UserNotFound(f"memory realm {memory_realm_id!r} not found")

            for existing in self._rebuild_jobs.values():
                if existing.memory_realm_id == memory_realm_id and existing.status in {
                    "pending",
                    "running",
                }:
                    raise RebuildAlreadyRunning(
                        "memory index rebuild for realm "
                        f"{memory_realm_id!r} is already {existing.status}"
                    )

            now = datetime.now(UTC)
            job_id = f"rebuild-{memory_realm_id}-{uuid.uuid4().hex[:10]}"
            log_path = self._maintenance_log_root / f"{job_id}.log"
            job = RebuildIndexJob(
                job_id=job_id,
                memory_realm_id=memory_realm_id,
                status="pending",
                created_at=now,
                log_path=log_path,
            )
            self._rebuild_jobs[job_id] = job
            task = asyncio.create_task(
                self._run_rebuild_index_job(job_id, entry),
                name=f"memory_rebuild_index:{memory_realm_id}",
            )
            self._rebuild_tasks[job_id] = task
            task.add_done_callback(lambda _task, jid=job_id: self._rebuild_tasks.pop(jid, None))
            return job.to_view()

    def get_rebuild_index_job(self, job_id: str) -> dict:
        job = self._rebuild_jobs.get(job_id)
        if job is None:
            raise RebuildJobNotFound(f"memory index rebuild job {job_id!r} not found")
        return job.to_view()

    def list_rebuild_index_jobs(self, *, memory_realm_id: str | None = None) -> list[dict]:
        jobs = self._rebuild_jobs.values()
        if memory_realm_id is not None:
            jobs = [j for j in jobs if j.memory_realm_id == memory_realm_id]
        return [j.to_view() for j in sorted(jobs, key=lambda j: j.created_at, reverse=True)]

    async def _run_rebuild_index_job(self, job_id: str, entry: UserEntry) -> None:
        job = self._rebuild_jobs[job_id]
        async with self._lock:
            job.status = "running"
            job.started_at = datetime.now(UTC)
        try:
            async with self._maintenance_lock:
                result = await self._sup.rebuild_memory_index(entry, log_path=job.log_path)
        except Exception as exc:  # noqa: BLE001 - async job must record failures
            async with self._lock:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = datetime.now(UTC)
            log.exception("memory_rebuild_index_failed", user_id=entry.id, job_id=job_id)
            return

        async with self._lock:
            job.result = result
            returncode = result.get("returncode")
            if returncode == 0:
                job.status = "succeeded"
                job.error = None
            else:
                job.status = "failed"
                job.error = f"mempalace repair exited with {returncode}"
            job.finished_at = datetime.now(UTC)

    # -------------------- create --------------------

    async def create_user(
        self,
        *,
        user_id: str,
        port: int | None = None,
        enabled: bool = False,
        palace_path: str = "",
        consolidator: ConsolidatorUserConfig | None = None,
        wait_for_worker_timeout_s: float = 10.0,
    ) -> dict:
        raise UserRegistryReadOnly(
            "memory no longer creates users; write users through eidolon_admin /api/users"
        )

    # -------------------- delete (cascade with compensation) --------------------

    async def delete_user(
        self,
        user_id: str,
        *,
        worker_stop_timeout_s: float = 30.0,
        purge_palace: bool = False,
    ) -> dict:
        """Cascade-delete a user with rollback on failure.

        The three steps, each individually safe:

        Step 1 — Reconcile
            Admin has already flipped enabled=false or deleted the registry
            row. Re-read the admin registry and wait for the worker to stop.

        Step 2 — Clean palace
            Move the user's palace directory under
            ``~/.eidolon-trash/<user>_<unix-ts>/`` by default, or delete it
            permanently when ``purge_palace=True``. If cleanup fails (FS
            error, permission), return 503 before memory reports success.

        Returns a small status dict so the operator UI can show what
        was actually done ("worker stopped, palace moved/deleted").
        """
        async with self._lock:
            config = load_users_config()
            entry = config.find(user_id)
            if entry is None:
                raise UserNotFound(f"user {user_id!r} not found")

            return await self._cleanup_runtime_and_palace(
                entry,
                worker_stop_timeout_s=worker_stop_timeout_s,
                purge_palace=purge_palace,
            )

    async def cleanup_orphaned_user(
        self,
        user_id: str,
        *,
        worker_stop_timeout_s: float = 30.0,
        purge_palace: bool = False,
    ) -> dict:
        """Clean palace data for a realm that is absent from admin's registry.

        This is the live-contract cleanup path after Admin/Data has already
        removed an owner tree. The registry row no longer exists, so
        ``delete_user`` cannot look up the port; palace resolution depends
        only on the realm id, so a synthetic disabled entry is sufficient.
        Active registry rows are rejected to avoid deleting a live realm.
        """
        async with self._lock:
            config = load_users_config()
            entry = config.find(user_id)
            if entry is not None and entry.enabled:
                raise UserStillRegistered(
                    f"user {user_id!r} is still enabled in admin registry; "
                    "disable or delete it before orphan cleanup"
                )
            cleanup_entry = entry or UserEntry(id=user_id, port=1, enabled=False)
            result = await self._cleanup_runtime_and_palace(
                cleanup_entry,
                worker_stop_timeout_s=worker_stop_timeout_s,
                purge_palace=purge_palace,
            )
            result["orphaned"] = entry is None
            return result

    # -------------------- internals --------------------

    async def _cleanup_runtime_and_palace(
        self,
        entry: UserEntry,
        *,
        worker_stop_timeout_s: float,
        purge_palace: bool,
    ) -> dict:
        user_id = entry.id
        palace_path = self._sup.palace_path_for(entry)

        # ---- Step 1: reconcile + await worker death ----
        await self._sup.reconcile_now()
        deadline = time.monotonic() + worker_stop_timeout_s
        while self._sup.is_worker_alive(user_id):
            if time.monotonic() >= deadline:
                log.error(
                    "user_admin_worker_did_not_stop",
                    user_id=user_id,
                    timeout_s=worker_stop_timeout_s,
                )
                raise WorkerNotTerminated(
                    f"worker for user {user_id!r} did not exit within "
                    f"{worker_stop_timeout_s}s; check worker log and retry"
                )
            await asyncio.sleep(0.1)

        # ---- Step 2: trash palace ----
        trash_target: Path | None = None
        palace_deleted = False
        deleted_logs: list[str] = []
        if palace_path.exists():
            try:
                if purge_palace:
                    self._delete_palace(palace_path, user_id)
                    palace_deleted = True
                    deleted_logs = self._delete_user_logs(user_id)
                else:
                    trash_target = self._trash_palace(palace_path, user_id)
            except Exception as exc:  # noqa: BLE001 - need broad to drive rollback
                log.exception("user_admin_palace_cleanup_failed", user_id=user_id)
                raise PalaceCleanupFailed(f"palace cleanup failed: {exc}") from exc

        # Final reconcile so any UI status reads are consistent.
        await self._sup.reconcile_now()

        return {
            "user_id": user_id,
            "deleted": True,
            "palace_trashed_to": str(trash_target) if trash_target else None,
            "palace_deleted": palace_deleted,
            "logs_deleted": deleted_logs,
        }

    def _trash_palace(self, palace_path: Path, user_id: str) -> Path:
        """Move palace_path under trash_root with a timestamp suffix.

        Returns the final trash directory path. Uses ``shutil.move`` which
        does a fast rename when source and dest are on the same filesystem
        (the common case since both default to under ``$HOME``).
        """
        self._trash_root.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        # Don't reuse a trash name if a previous delete put one there — append
        # a counter. Cheap because the directory listing is small.
        target = self._trash_root / f"{user_id}_{stamp}"
        counter = 1
        while target.exists():
            target = self._trash_root / f"{user_id}_{stamp}_{counter}"
            counter += 1
        shutil.move(str(palace_path), str(target))
        log.info(
            "user_admin_palace_trashed",
            user_id=user_id,
            from_=str(palace_path),
            to=str(target),
        )
        return target

    def _delete_palace(self, palace_path: Path, user_id: str) -> None:
        shutil.rmtree(palace_path)
        log.info(
            "user_admin_palace_deleted",
            user_id=user_id,
            path=str(palace_path),
        )

    def _delete_user_logs(self, user_id: str) -> list[str]:
        deleted: list[str] = []
        for name in (f"agent_{user_id}.log", f"consolidator_{user_id}.log"):
            path = self._user_log_root / name
            if not path.exists():
                continue
            path.unlink()
            deleted.append(str(path))
        if deleted:
            log.info("user_admin_logs_deleted", user_id=user_id, files=deleted)
        return deleted
