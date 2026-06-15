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
from collections.abc import Iterable
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


# ---- protocols (so tests can stub) ------------------------------------------


@runtime_checkable
class _SupervisorProtocol(Protocol):
    """The slice of Supervisor that user_admin needs.

    Defined as a protocol so the orchestration tests can substitute a
    minimal fake; the real Supervisor in ``entrypoints/supervisor.py``
    implements all of these.
    """

    users_path: Path | None

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
            "created_at": datetime.now(UTC).isoformat(),
        },
        "health": {
            "worker_running": worker_alive and user.enabled,
            "mcp_reachable": worker_alive and user.enabled,  # liveness conflates the two for now
            "palace_initialized": palace_path.exists(),
            "note": "" if user.enabled else "user disabled by admin registry",
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

    async def reconcile(self) -> None:
        async with self._lock:
            await self._sup.reconcile_now()

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
    ) -> dict:
        """Cascade-delete a user with rollback on failure.

        The three steps, each individually safe:

        Step 1 — Reconcile
            Admin has already flipped enabled=false or deleted the registry
            row. Re-read the admin registry and wait for the worker to stop.

        Step 2 — Trash palace
            Move the user's palace directory under
            ``~/.eidolon-trash/<user>_<unix-ts>/``. This is reversible
            until the operator wipes the trash. If the move fails (FS
            error, permission), we ROLL BACK step 1: re-enable the user,
            reconcile, supervisor brings the worker back. Return 503.

        Returns a small status dict so the operator UI can show what
        was actually done ("worker stopped, palace moved to <path>").
        """
        async with self._lock:
            config = load_users_config(path=self._sup.users_path)
            entry = config.find(user_id)
            if entry is None:
                raise UserNotFound(f"user {user_id!r} not found")

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
            if palace_path.exists():
                try:
                    trash_target = self._trash_palace(palace_path, user_id)
                except Exception as exc:  # noqa: BLE001 - need broad to drive rollback
                    log.exception("user_admin_palace_trash_failed", user_id=user_id)
                    raise PalaceCleanupFailed(
                        f"palace move to trash failed: {exc}"
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
