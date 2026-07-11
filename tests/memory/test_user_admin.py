"""Tests for the memory user control plane.

Real file system (tmp_path for palace dirs + trash). Stub supervisor so we
can drive worker_alive deterministically and verify the
cleanup path WITHOUT spawning real subprocesses.

Memory no longer owns the user registry. It reads admin's registry, exposes
reconcile for runtime sync, and deletes only memory-owned palace data after
admin has disabled or removed a user.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eidolon.memory.application import user_admin as user_admin_mod
from eidolon.memory.application.user_admin import (
    PalaceCleanupFailed,
    PortConflict,
    RebuildAlreadyRunning,
    UserAdmin,
    UserNotFound,
    UserRegistryReadOnly,
    UserStillRegistered,
    WorkerNotTerminated,
    allocate_port,
)
from eidolon.memory.config.users import UserEntry, UsersConfig

# ---- stub supervisor --------------------------------------------------------


class _StubSupervisor:
    """Implements the _SupervisorProtocol slice user_admin depends on.

    State-machine model:
      - ``alive``: which user_ids the stub claims have running workers
      - on ``reconcile_now()``: re-read registry snapshot, sync ``alive`` against
        ``enabled_users``. This is the contract real Supervisor obeys:
        after reconcile, enabled users have workers, disabled don't.

    Behavior toggles for adversarial cases:
      - ``reconcile_terminates_worker = False`` → reconcile does NOT
        remove workers (simulates a wedged worker). Used to trigger
        the WorkerNotTerminated branch.
    """

    def __init__(self, palaces_root: Path) -> None:
        self._palaces_root = palaces_root
        self.registry = UsersConfig(users=[])
        self.alive: set[str] = set()
        self.reconcile_terminates_worker: bool = True
        self.reconcile_count: int = 0
        self.rebuild_calls: list[tuple[str, Path]] = []
        self.rebuild_returncode: int = 0
        self.rebuild_wait: asyncio.Event | None = None

    async def reconcile_now(self) -> None:
        self.reconcile_count += 1
        cfg = self.registry
        enabled_ids = {u.id for u in cfg.users if u.enabled}
        # bring enabled workers up
        self.alive |= enabled_ids
        # take disabled / removed workers down (unless we're simulating a
        # wedged worker that won't die)
        if self.reconcile_terminates_worker:
            self.alive -= {uid for uid in list(self.alive) if uid not in enabled_ids}

    def is_worker_alive(self, user_id: str) -> bool:
        return user_id in self.alive

    def palace_path_for(self, user: UserEntry) -> Path:
        return self._palaces_root / user.id

    def palace_initialized(self, user: UserEntry) -> bool:
        return self.palace_path_for(user).exists()

    async def rebuild_memory_index(self, user: UserEntry, *, log_path: Path) -> dict:
        self.rebuild_calls.append((user.id, log_path))
        if self.rebuild_wait is not None:
            await self.rebuild_wait.wait()
        return {
            "user_id": user.id,
            "palace_path": str(self.palace_path_for(user)),
            "backend": "chroma",
            "returncode": self.rebuild_returncode,
            "log_path": str(log_path),
        }


def _set_users(sup: _StubSupervisor, *entries: UserEntry) -> None:
    sup.registry = UsersConfig(users=list(entries))


@pytest.fixture
def admin_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[UserAdmin, _StubSupervisor, Path]:
    palaces_root = tmp_path / "palaces"
    trash_root = tmp_path / "trash"
    sup = _StubSupervisor(palaces_root)
    monkeypatch.setattr(user_admin_mod, "load_users_config", lambda: sup.registry)
    admin = UserAdmin(
        sup,
        trash_root=trash_root,
        maintenance_log_root=tmp_path / "maintenance",
        user_log_root=tmp_path / "logs",
    )
    return admin, sup, tmp_path


# ---- port allocation --------------------------------------------------------


def test_allocate_port_picks_lowest_free() -> None:
    existing = [
        UserEntry(id="a", port=10030),
        UserEntry(id="b", port=10032),
    ]
    assert allocate_port(existing) == 10031


def test_allocate_port_full_range_raises() -> None:
    # Fill the whole [10030, 10100) legacy admin allocation range.
    existing = [UserEntry(id=f"u{p}", port=p) for p in range(10030, 10100)]
    with pytest.raises(PortConflict):
        allocate_port(existing)


# ---- read-only registry boundary ------------------------------------------


async def test_create_user_is_read_only(admin_env) -> None:
    admin, sup, _ = admin_env
    with pytest.raises(UserRegistryReadOnly):
        await admin.create_user(user_id="alice")
    assert sup.registry.users == []
    assert sup.reconcile_count == 0


async def test_reconcile_delegates_to_supervisor(admin_env) -> None:
    admin, sup, _ = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=True))
    await admin.reconcile()
    assert "alice" in sup.alive
    assert sup.reconcile_count == 1


async def test_rebuild_index_job_succeeds(admin_env) -> None:
    admin, sup, _ = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=True))

    created = await admin.start_rebuild_index("alice")
    assert created["status"] == "pending"
    job_id = created["job_id"]

    for _ in range(20):
        status = admin.get_rebuild_index_job(job_id)
        if status["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)

    status = admin.get_rebuild_index_job(job_id)
    assert status["status"] == "succeeded"
    assert status["error"] is None
    assert status["result"]["returncode"] == 0
    assert sup.rebuild_calls[0][0] == "alice"


async def test_rebuild_index_job_failure_is_recorded(admin_env) -> None:
    admin, sup, _ = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=True))
    sup.rebuild_returncode = 2

    created = await admin.start_rebuild_index("alice")
    job_id = created["job_id"]

    for _ in range(20):
        status = admin.get_rebuild_index_job(job_id)
        if status["status"] == "failed":
            break
        await asyncio.sleep(0.01)

    status = admin.get_rebuild_index_job(job_id)
    assert status["status"] == "failed"
    assert "exited with 2" in status["error"]


async def test_rebuild_index_rejects_duplicate_running_job(admin_env) -> None:
    admin, sup, _ = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=True))
    sup.rebuild_wait = asyncio.Event()

    created = await admin.start_rebuild_index("alice")
    for _ in range(20):
        if admin.get_rebuild_index_job(created["job_id"])["status"] == "running":
            break
        await asyncio.sleep(0.01)

    with pytest.raises(RebuildAlreadyRunning):
        await admin.start_rebuild_index("alice")

    sup.rebuild_wait.set()
    for _ in range(20):
        if admin.get_rebuild_index_job(created["job_id"])["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)


# ---- delete cleanup path ---------------------------------------------------


async def test_delete_user_three_step_happy_path(admin_env) -> None:
    admin, sup, tmp_path = admin_env
    # Seed: admin has already disabled alice; a worker is still alive until
    # reconcile observes the registry and terminates it.
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=False))
    sup.alive.add("alice")
    palace = tmp_path / "palaces" / "alice"
    palace.mkdir(parents=True)
    (palace / "chroma.sqlite3").write_bytes(b"some data")

    result = await admin.delete_user("alice")

    # Step 1: worker terminated.
    assert "alice" not in sup.alive
    # Step 2: palace moved to trash, original gone.
    assert not palace.exists()
    assert result["palace_trashed_to"] is not None
    assert result["palace_deleted"] is False
    trash_dir = Path(result["palace_trashed_to"])
    assert trash_dir.exists()
    assert (trash_dir / "chroma.sqlite3").read_bytes() == b"some data"
    # Registry ownership stays with admin; memory does not remove the row.
    assert len(sup.registry.users) == 1
    assert sup.registry.users[0].enabled is False


async def test_delete_user_purge_removes_palace(admin_env) -> None:
    admin, sup, tmp_path = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=False))
    sup.alive.add("alice")
    palace = tmp_path / "palaces" / "alice"
    palace.mkdir(parents=True)
    (palace / "chroma.sqlite3").write_bytes(b"some data")
    logs = tmp_path / "logs"
    logs.mkdir()
    agent_log = logs / "agent_alice.log"
    consolidator_log = logs / "consolidator_alice.log"
    agent_log.write_text("agent log")
    consolidator_log.write_text("consolidator log")

    result = await admin.delete_user("alice", purge_palace=True)

    assert "alice" not in sup.alive
    assert not palace.exists()
    assert not agent_log.exists()
    assert not consolidator_log.exists()
    assert result["palace_trashed_to"] is None
    assert result["palace_deleted"] is True
    assert sorted(Path(p).name for p in result["logs_deleted"]) == [
        "agent_alice.log",
        "consolidator_alice.log",
    ]


async def test_delete_user_missing_raises_404(admin_env) -> None:
    admin, _, _ = admin_env
    with pytest.raises(UserNotFound):
        await admin.delete_user("ghost")


# ---- cascade compensation --------------------------------------------------


async def test_delete_user_rolls_back_when_worker_wont_stop(admin_env) -> None:
    """Step 1 hangs (worker refuses to die) → raise without mutating registry."""
    admin, sup, _ = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=False))
    sup.alive.add("alice")
    sup.reconcile_terminates_worker = False  # simulate wedged worker

    with pytest.raises(WorkerNotTerminated):
        await admin.delete_user("alice", worker_stop_timeout_s=0.1)

    assert len(sup.registry.users) == 1
    assert sup.registry.users[0].enabled is False
    # Worker (in the simulation) is still alive.
    assert "alice" in sup.alive


async def test_delete_user_rolls_back_when_palace_trash_fails(
    admin_env, monkeypatch
) -> None:
    """Step 2 fails (FS error moving palace) → worker remains stopped."""
    admin, sup, tmp_path = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=False))
    sup.alive.add("alice")
    palace = tmp_path / "palaces" / "alice"
    palace.mkdir(parents=True)
    (palace / "x").write_text("data")

    def _explode(*_args, **_kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(
        "eidolon.memory.application.user_admin.shutil.move", _explode
    )

    with pytest.raises(PalaceCleanupFailed):
        await admin.delete_user("alice")

    assert sup.registry.users[0].enabled is False
    assert "alice" not in sup.alive
    # Palace still intact on disk (move never happened).
    assert palace.exists()
    assert (palace / "x").read_text() == "data"


async def test_delete_user_without_palace_returns_success(admin_env) -> None:
    admin, sup, tmp_path = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=False))
    result = await admin.delete_user("alice")
    assert result == {
        "user_id": "alice",
        "deleted": True,
        "palace_trashed_to": None,
        "palace_deleted": False,
        "logs_deleted": [],
    }
    assert not (tmp_path / "palaces" / "alice").exists()
    assert "alice" not in sup.alive
    assert len(sup.registry.users) == 1
    assert sup.registry.users[0].enabled is False


async def test_cleanup_orphaned_user_purges_absent_registry_palace(admin_env) -> None:
    admin, sup, tmp_path = admin_env
    palace = tmp_path / "palaces" / "orphan"
    palace.mkdir(parents=True)
    (palace / "chroma.sqlite3").write_bytes(b"some data")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent_orphan.log").write_text("agent log")

    result = await admin.cleanup_orphaned_user("orphan", purge_palace=True)

    assert result["user_id"] == "orphan"
    assert result["orphaned"] is True
    assert result["palace_deleted"] is True
    assert not palace.exists()
    assert not (logs / "agent_orphan.log").exists()
    assert sup.reconcile_count == 2


async def test_cleanup_orphaned_user_rejects_enabled_registry_entry(admin_env) -> None:
    admin, sup, _ = admin_env
    _set_users(sup, UserEntry(id="alice", port=8030, enabled=True))

    with pytest.raises(UserStillRegistered):
        await admin.cleanup_orphaned_user("alice", purge_palace=True)

    assert sup.reconcile_count == 0


# ---- list / get -----------------------------------------------------------


async def test_list_users_returns_health_per_user(admin_env) -> None:
    admin, sup, tmp_path = admin_env
    _set_users(
        sup,
        UserEntry(id="alice", port=8030, enabled=True),
        UserEntry(id="bob", port=8031, enabled=False),
    )
    sup.alive.add("alice")  # bob is disabled, no worker
    (tmp_path / "palaces" / "alice").mkdir(parents=True)

    views = admin.list_users()
    by_id = {v["spec"]["memory_realm_id"]: v for v in views}
    assert by_id["alice"]["health"]["worker_running"] is True
    assert by_id["alice"]["health"]["palace_initialized"] is True
    assert by_id["bob"]["health"]["worker_running"] is False
    # Disabled user has the note explaining why worker_running is false.
    assert "disabled" in by_id["bob"]["health"]["note"]


async def test_get_user_missing_raises(admin_env) -> None:
    admin, _, _ = admin_env
    with pytest.raises(UserNotFound):
        admin.get_user("ghost")
