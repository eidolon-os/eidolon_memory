"""User CRUD orchestration with cascade-delete compensation.

Sits between the HTTP layer (``entrypoints/admin_api.py``) and the
process layer (``entrypoints/supervisor.py``). Knows:

  * how to allocate a free MCP port for a new user (scans users.yaml)
  * how to drive the supervisor through a state transition without
    racing its own reconcile loop (serialized via an asyncio.Lock)
  * how to roll back a partial DELETE if any step fails — so a failed
    palace deletion does NOT leave the user half-alive (worker dead,
    yaml entry still there, palace still on disk)

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Protocol, runtime_checkable

from eidolon.memory.config.users import (
    ConsolidatorUserConfig,
    UserEntry,
    UsersConfig,
    load_users_config,
)
from eidolon.memory.config.users_io import (
    UsersYamlError,
    remove_user as yaml_remove_user,
    update_enabled as yaml_update_enabled,
    upsert_user as yaml_upsert_user,
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
    """File-system error while moving the palace to trash. Step 1 was
    rolled back: worker is restored, yaml entry is enabled again.
    """

    status_code = 503


# ---- protocols (so tests can stub) ------------------------------------------


@runtime_checkable
class _SupervisorProtocol(Protocol):
    """The slice of Supervisor that user_admin needs.

    Defined as a protocol so the orchestration tests can substitute a
    minimal fake; the real Supervisor in ``entrypoints/supervisor.py``
    implements all of these.
    """

    users_path: Path  # absolute path to users.yaml

    async def reconcile_now(self) -> None:
        """Re-read users.yaml and align running children. Must be safe to
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


# ---- port allocation --------------------------------------------------------


# Auto-allocate range for newly-created users. Operators can still explicitly
# specify a port at create-time; this range is only used when they don't.
# 8030 is the default first-user port; we leave a 70-port window. If you need
# more than 70 users you should switch to specifying ports explicitly anyway.
_AUTO_PORT_MIN = 8030
_AUTO_PORT_MAX = 8100


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
    user: UserEntry, *, worker_alive: bool, palace_path: Path
) -> dict:
    """The flat JSON shape the HTTP layer returns. Matches admin's
    ``UserView`` schema field-for-field — admin can ``model_validate(view)``
    on this dict.

    ``mcp_http_url`` (added 29.K): the user-worker's MCP endpoint.
    Memory is authoritative for port assignment, so we expose the URL
    here rather than make admin synthesize from convention. Channel
    eventually receives this via /api/resolve and dials it for tools.
    """
    return {
        "spec": {
            "user_id": user.id,
            # memory has no tenant concept — admin tags users with a tenant
            # at its own bookkeeping layer; memory always returns "default".
            "tenant_id": "default",
            "display_name": user.id,  # memory has no display name field today
            "enabled": user.enabled,
            "palace_path": user.palace_path,
            "consolidator": {
                "enabled": user.consolidator.enabled if user.consolidator else False,
                "interval_hours": user.consolidator.interval_hours if user.consolidator else 6.0,
                "window_days": user.consolidator.window_days if user.consolidator else 30,
                "min_drawers": user.consolidator.min_drawers if user.consolidator else 3,
                "min_confidence": user.consolidator.min_confidence if user.consolidator else 0.6,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "health": {
            "worker_running": worker_alive and user.enabled,
            "mcp_reachable": worker_alive and user.enabled,  # liveness conflates the two for now
            "palace_initialized": palace_path.exists(),
            "note": "" if user.enabled else "user disabled (yaml enabled=false)",
        },
        "active_agent_id": None,  # admin-side concept, memory doesn't know
        "agent_ids": [],  # ditto
        # Runtime addressing — admin needs this to compose ResolvedContext
        # for channel without a second round-trip. Memory's MCP path is
        # always /mcp and the host is loopback (sub-projects co-locate
        # with admin in the dev stack).
        "mcp_http_url": f"http://127.0.0.1:{user.port}/mcp",
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
    ) -> None:
        self._sup = supervisor
        self._lock = asyncio.Lock()
        # Where ``delete_user`` moves a palace before final yaml removal.
        # Putting trash in a sibling of the palaces root keeps it on the same
        # filesystem (so ``shutil.move`` stays atomic via ``os.rename``).
        if trash_root is None:
            # Default: ~/.eidolon-trash — discoverable, not under ~/eidolon/
            # so a wipe-all-palaces command never accidentally erases trash.
            trash_root = Path.home() / ".eidolon-trash"
        self._trash_root = trash_root

    # -------------------- list / get --------------------

    def list_users(self) -> list[dict]:
        config = load_users_config(path=self._sup.users_path)
        return [
            user_to_view(
                u,
                worker_alive=self._sup.is_worker_alive(u.id),
                palace_path=self._sup.palace_path_for(u),
            )
            for u in config.users
        ]

    def get_user(self, user_id: str) -> dict:
        config = load_users_config(path=self._sup.users_path)
        user = config.find(user_id)
        if user is None:
            raise UserNotFound(f"user {user_id!r} not found")
        return user_to_view(
            user,
            worker_alive=self._sup.is_worker_alive(user.id),
            palace_path=self._sup.palace_path_for(user),
        )

    # -------------------- create --------------------

    async def create_user(
        self,
        *,
        user_id: str,
        port: Optional[int] = None,
        enabled: bool = False,
        palace_path: str = "",
        consolidator: Optional[ConsolidatorUserConfig] = None,
        wait_for_worker_timeout_s: float = 10.0,
    ) -> dict:
        """Create a user, persist to yaml, and optionally start its worker.

        Steps:
          1. validate uniqueness (id + port)
          2. write the new entry to users.yaml (atomic + locked)
          3. if enabled: trigger reconcile (in-process, no SIGHUP)
          4. if enabled: poll for the worker to be alive within
             ``wait_for_worker_timeout_s``
          5. return the freshly built view. Disabled users are pure catalog
             records until they are explicitly enabled.

        Failure modes:
          - id collision           → 409 UserAlreadyExists, no yaml change
          - port collision         → 409 PortConflict, no yaml change
          - yaml write failure     → original yaml intact (atomic write)
          - reconcile/worker hang  → 503; the yaml entry remains, operator
                                     can investigate the worker log
        """
        async with self._lock:
            config = load_users_config(path=self._sup.users_path)
            if config.find(user_id) is not None:
                raise UserAlreadyExists(f"user {user_id!r} already in users.yaml")

            # Port allocation: explicit > auto-pick from range
            if port is not None:
                if any(u.port == port for u in config.users):
                    raise PortConflict(f"port {port} already used by another user")
                final_port = port
            else:
                final_port = allocate_port(config.users)

            entry = UserEntry(
                id=user_id,
                port=final_port,
                enabled=enabled,
                palace_path=palace_path,
                consolidator=consolidator,
            )

            try:
                yaml_upsert_user(self._sup.users_path, entry)
            except UsersYamlError as exc:
                # UsersConfig's validator caught a duplicate-port-against-enabled
                # case the in-memory check missed (e.g. concurrent writer).
                raise PortConflict(str(exc)) from exc

            if enabled:
                await self._sup.reconcile_now()
                # Wait for the worker process to be alive. We DON'T poll the MCP
                # port from here because that introduces an HTTP roundtrip into
                # the supervisor's event loop; admin's later resolve endpoint
                # does the MCP liveness check.
                deadline = time.monotonic() + wait_for_worker_timeout_s
                while not self._sup.is_worker_alive(user_id):
                    if time.monotonic() >= deadline:
                        log.warning(
                            "user_admin_create_worker_slow",
                            user_id=user_id,
                            timeout_s=wait_for_worker_timeout_s,
                        )
                        # Don't roll back — the entry is valid, the worker may
                        # still be starting (palace init is the slow part).
                        # Return the view with worker_running=false so the
                        # operator sees the degraded state and can decide.
                        break
                    await asyncio.sleep(0.1)

            return user_to_view(
                entry,
                worker_alive=self._sup.is_worker_alive(user_id),
                palace_path=self._sup.palace_path_for(entry),
            )

    # -------------------- delete (cascade with compensation) --------------------

    async def delete_user(
        self,
        user_id: str,
        *,
        worker_stop_timeout_s: float = 30.0,
    ) -> dict:
        """Cascade-delete a user with rollback on failure.

        The three steps, each individually safe:

        Step 1 — Disable
            Flip ``enabled=false`` in users.yaml, trigger reconcile. The
            supervisor terminates the agent_runner (and consolidator if
            any) with grace. If reconcile or the worker hangs, we return
            503 with the yaml *not* fully disabled — re-running DELETE is
            idempotent.

        Step 2 — Trash palace
            Move the user's palace directory under
            ``~/.eidolon-trash/<user>_<unix-ts>/``. This is reversible
            until the operator wipes the trash. If the move fails (FS
            error, permission), we ROLL BACK step 1: re-enable the user,
            reconcile, supervisor brings the worker back. Return 503.

        Step 3 — Remove yaml entry
            Permanently remove the user from users.yaml. After this,
            the user no longer exists. If THIS step fails (rare — yaml
            disk full?), the worker is dead, palace is trashed, but
            the entry remains as ``enabled=false``. Subsequent DELETE
            is idempotent: yaml entry gone, palace already trashed
            (idempotent move), worker already dead.

        Returns a small status dict so the operator UI can show what
        was actually done ("worker stopped, palace moved to <path>").
        """
        async with self._lock:
            config = load_users_config(path=self._sup.users_path)
            entry = config.find(user_id)
            if entry is None:
                raise UserNotFound(f"user {user_id!r} not found")

            palace_path = self._sup.palace_path_for(entry)
            was_enabled = entry.enabled

            # ---- Step 1: disable + reconcile, await worker death ----
            if entry.enabled:
                try:
                    yaml_update_enabled(self._sup.users_path, user_id, enabled=False)
                except UsersYamlError as exc:
                    raise UserAdminError(f"step 1 yaml write failed: {exc}") from exc

                await self._sup.reconcile_now()

                # Wait for the worker to actually terminate.
                deadline = time.monotonic() + worker_stop_timeout_s
                while self._sup.is_worker_alive(user_id):
                    if time.monotonic() >= deadline:
                        # Step 1 didn't complete. Try to roll back, but the
                        # worker being stuck means it's already in a bad state.
                        log.error(
                            "user_admin_worker_did_not_stop",
                            user_id=user_id,
                            timeout_s=worker_stop_timeout_s,
                        )
                        # Best-effort rollback of yaml. The worker is still
                        # alive so functionally nothing changed for users.
                        if was_enabled:
                            try:
                                yaml_update_enabled(
                                    self._sup.users_path, user_id, enabled=True,
                                )
                            except Exception:
                                log.exception(
                                    "user_admin_rollback_failed",
                                    user_id=user_id,
                                )
                        raise WorkerNotTerminated(
                            f"worker for user {user_id!r} did not exit within "
                            f"{worker_stop_timeout_s}s; check worker log and retry"
                        )
                    await asyncio.sleep(0.1)

            # ---- Step 2: trash palace ----
            trash_target: Path | None = None
            if palace_path.exists():
                try:
                    trash_target = self._trash_palace(palace_path, user_id)
                except Exception as exc:  # noqa: BLE001 - need broad to drive rollback
                    log.exception("user_admin_palace_trash_failed", user_id=user_id)
                    # Roll back step 1: re-enable, reconcile, supervisor
                    # brings the worker back up.
                    if was_enabled:
                        try:
                            yaml_update_enabled(
                                self._sup.users_path, user_id, enabled=True,
                            )
                            await self._sup.reconcile_now()
                        except Exception:
                            log.exception(
                                "user_admin_rollback_after_palace_fail",
                                user_id=user_id,
                            )
                    raise PalaceCleanupFailed(
                        f"palace move to trash failed: {exc}"
                    ) from exc

            # ---- Step 3: final yaml removal (point of no return) ----
            try:
                yaml_remove_user(self._sup.users_path, user_id)
            except UsersYamlError as exc:
                # Worker is dead, palace is trashed. Yaml entry remains
                # as enabled=false. Operator can retry DELETE — it's
                # idempotent (find returns the stale entry, worker
                # check passes immediately, palace.exists() is False so
                # step 2 skips, step 3 retries).
                log.warning(
                    "user_admin_yaml_final_remove_failed",
                    user_id=user_id,
                    error=str(exc),
                )
                raise UserAdminError(
                    f"user worker stopped and palace trashed to {trash_target}, "
                    f"but yaml entry remove failed: {exc} — re-run DELETE to clean up"
                ) from exc

            # Final reconcile so any UI status reads are consistent.
            await self._sup.reconcile_now()

            return {
                "user_id": user_id,
                "deleted": True,
                "palace_trashed_to": str(trash_target) if trash_target else None,
            }

    # -------------------- internals --------------------

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
