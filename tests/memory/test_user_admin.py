"""Tests for the user-admin control plane + cascade delete compensation.

Real file system (tmp_path for users.yaml + palace dirs + trash). Stub
supervisor so we can drive worker_alive deterministically and verify the
cascade's rollback path WITHOUT spawning real subprocesses.

The 4-step DELETE flow needs adversarial coverage:
  - happy path: all three steps succeed
  - worker doesn't terminate within timeout → rollback yaml, raise 503
  - palace trash fails → rollback yaml AND reconcile, raise 503
  - yaml final-remove fails after palace trashed → raise 503 but worker
    is dead and palace is gone (intentional non-rollback; DELETE retry
    is idempotent)

CREATE flow tests:
  - allocate port when not specified
  - reject duplicate user_id
  - reject port collision (against any user, not just enabled)
  - worker-slow-to-start returns view with worker_running=false
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from eidolon.memory.application.user_admin import (
    PalaceCleanupFailed,
    PortConflict,
    UserAdmin,
    UserAdminError,
    UserAlreadyExists,
    UserNotFound,
    WorkerNotTerminated,
    allocate_port,
)
from eidolon.memory.config.users import UserEntry, UsersConfig


# ---- stub supervisor --------------------------------------------------------


class _StubSupervisor:
    """Implements the _SupervisorProtocol slice user_admin depends on.

    State-machine model:
      - ``alive``: which user_ids the stub claims have running workers
      - on ``reconcile_now()``: re-read yaml, sync ``alive`` against
        ``enabled_users``. This is the contract real Supervisor obeys:
        after reconcile, enabled users have workers, disabled don't.

    Behavior toggles for adversarial cases:
      - ``reconcile_terminates_worker = False`` → reconcile does NOT
        remove workers (simulates a wedged worker). Used to trigger
        the WorkerNotTerminated branch.
    """

    def __init__(self, users_path: Path, palaces_root: Path) -> None:
        self.users_path = users_path
        self._palaces_root = palaces_root
        self.alive: set[str] = set()
        self.reconcile_terminates_worker: bool = True
        self.reconcile_count: int = 0

    async def reconcile_now(self) -> None:
        self.reconcile_count += 1
        cfg = _read_users(self.users_path)
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


# ---- helpers ----------------------------------------------------------------


def _read_users(path: Path) -> UsersConfig:
    if not path.is_file():
        return UsersConfig(users=[])
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return UsersConfig.model_validate(raw)


def _yaml_init(path: Path, *entries: UserEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"users": [u.model_dump(mode="json") for u in entries]}
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def admin_env(tmp_path: Path) -> tuple[UserAdmin, _StubSupervisor, Path]:
    users_yaml = tmp_path / "config" / "users.yaml"
    palaces_root = tmp_path / "palaces"
    trash_root = tmp_path / "trash"
    _yaml_init(users_yaml)  # start empty
    sup = _StubSupervisor(users_yaml, palaces_root)
    admin = UserAdmin(sup, trash_root=trash_root)
    return admin, sup, tmp_path


# ---- port allocation --------------------------------------------------------


def test_allocate_port_picks_lowest_free() -> None:
    existing = [
        UserEntry(id="a", port=8030),
        UserEntry(id="b", port=8032),
    ]
    assert allocate_port(existing) == 8031


def test_allocate_port_full_range_raises() -> None:
    # Fill the whole [8030, 8100) range
    existing = [UserEntry(id=f"u{p}", port=p) for p in range(8030, 8100)]
    with pytest.raises(PortConflict):
        allocate_port(existing)


# ---- create happy path -----------------------------------------------------


async def test_create_user_persists_disabled_by_default(admin_env) -> None:
    admin, sup, _ = admin_env
    view = await admin.create_user(user_id="alice")

    # Yaml has alice now.
    cfg = _read_users(sup.users_path)
    assert [u.id for u in cfg.users] == ["alice"]
    # Auto-allocated port from the [8030, 8100) range.
    assert cfg.users[0].port == 8030
    assert cfg.users[0].enabled is False
    # Default creation only persists config; activation is a separate step.
    assert "alice" not in sup.alive
    assert view["spec"]["user_id"] == "alice"
    assert view["spec"]["enabled"] is False
    assert view["health"]["worker_running"] is False
    assert sup.reconcile_count == 0


async def test_create_user_enabled_starts_worker(admin_env) -> None:
    admin, sup, _ = admin_env
    view = await admin.create_user(user_id="alice", enabled=True)

    cfg = _read_users(sup.users_path)
    assert cfg.users[0].enabled is True
    assert "alice" in sup.alive
    assert view["spec"]["enabled"] is True
    assert view["health"]["worker_running"] is True
    assert sup.reconcile_count == 1


async def test_create_user_explicit_port(admin_env) -> None:
    admin, sup, _ = admin_env
    await admin.create_user(user_id="alice", port=8050)
    cfg = _read_users(sup.users_path)
    assert cfg.users[0].port == 8050


async def test_create_user_rejects_duplicate_id(admin_env) -> None:
    admin, sup, _ = admin_env
    _yaml_init(sup.users_path, UserEntry(id="alice", port=8030))
    with pytest.raises(UserAlreadyExists):
        await admin.create_user(user_id="alice")


async def test_create_user_rejects_port_collision(admin_env) -> None:
    admin, sup, _ = admin_env
    _yaml_init(sup.users_path, UserEntry(id="alice", port=8030))
    with pytest.raises(PortConflict):
        await admin.create_user(user_id="bob", port=8030)


# ---- delete happy path -----------------------------------------------------


async def test_delete_user_three_step_happy_path(admin_env) -> None:
    admin, sup, tmp_path = admin_env
    # Seed: alice exists, worker alive, palace on disk.
    _yaml_init(sup.users_path, UserEntry(id="alice", port=8030))
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
    trash_dir = Path(result["palace_trashed_to"])
    assert trash_dir.exists()
    assert (trash_dir / "chroma.sqlite3").read_bytes() == b"some data"
    # Step 3: yaml entry gone.
    cfg = _read_users(sup.users_path)
    assert cfg.users == []


async def test_delete_user_missing_raises_404(admin_env) -> None:
    admin, _, _ = admin_env
    with pytest.raises(UserNotFound):
        await admin.delete_user("ghost")


# ---- cascade compensation --------------------------------------------------


async def test_delete_user_rolls_back_when_worker_wont_stop(admin_env) -> None:
    """Step 1 hangs (worker refuses to die) → yaml flipped back to enabled=true."""
    admin, sup, _ = admin_env
    _yaml_init(sup.users_path, UserEntry(id="alice", port=8030, enabled=True))
    sup.alive.add("alice")
    sup.reconcile_terminates_worker = False  # simulate wedged worker

    with pytest.raises(WorkerNotTerminated):
        await admin.delete_user("alice", worker_stop_timeout_s=0.1)

    # Yaml should be rolled back to enabled=true.
    cfg = _read_users(sup.users_path)
    assert len(cfg.users) == 1
    assert cfg.users[0].enabled is True
    # Worker (in the simulation) is still alive.
    assert "alice" in sup.alive


async def test_delete_user_rolls_back_when_palace_trash_fails(
    admin_env, monkeypatch
) -> None:
    """Step 2 fails (FS error moving palace) → step 1 rolled back: yaml
    re-enabled, supervisor reconciled, worker brought back up."""
    admin, sup, tmp_path = admin_env
    _yaml_init(sup.users_path, UserEntry(id="alice", port=8030, enabled=True))
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

    # Yaml re-enabled.
    cfg = _read_users(sup.users_path)
    assert cfg.users[0].enabled is True
    # Worker brought back up by the rollback reconcile.
    assert "alice" in sup.alive
    # Palace still intact on disk (move never happened).
    assert palace.exists()
    assert (palace / "x").read_text() == "data"


async def test_delete_user_does_not_rollback_after_palace_trashed(
    admin_env, monkeypatch
) -> None:
    """Step 3 (final yaml remove) fails — palace ALREADY trashed, worker
    dead. We don't try to un-trash; we raise 503 with a "retry DELETE"
    message and the operator re-runs (idempotent)."""
    admin, sup, tmp_path = admin_env
    _yaml_init(sup.users_path, UserEntry(id="alice", port=8030, enabled=True))
    sup.alive.add("alice")
    palace = tmp_path / "palaces" / "alice"
    palace.mkdir(parents=True)
    (palace / "x").write_text("data")

    # Make the FINAL yaml remove blow up.
    real_remove = __import__(
        "eidolon.memory.config.users_io", fromlist=["remove_user"]
    ).remove_user

    def _explode_remove(*_args, **_kwargs):
        raise __import__(
            "eidolon.memory.config.users_io", fromlist=["UsersYamlError"]
        ).UsersYamlError("simulated yaml disk full")

    monkeypatch.setattr(
        "eidolon.memory.application.user_admin.yaml_remove_user", _explode_remove
    )

    with pytest.raises(UserAdminError) as exc_info:
        await admin.delete_user("alice")

    # The error message tells the operator about the retry path.
    assert "re-run DELETE" in str(exc_info.value)
    # Palace is gone (step 2 succeeded).
    assert not palace.exists()
    # Worker is dead.
    assert "alice" not in sup.alive
    # Yaml still has alice as disabled (step 3 didn't commit).
    cfg = _read_users(sup.users_path)
    assert len(cfg.users) == 1
    assert cfg.users[0].enabled is False

    # And: re-running DELETE is idempotent — should drive yaml clean.
    monkeypatch.setattr(
        "eidolon.memory.application.user_admin.yaml_remove_user", real_remove
    )
    result = await admin.delete_user("alice")
    assert result["deleted"] is True
    cfg = _read_users(sup.users_path)
    assert cfg.users == []


# ---- list / get -----------------------------------------------------------


async def test_list_users_returns_health_per_user(admin_env) -> None:
    admin, sup, tmp_path = admin_env
    _yaml_init(
        sup.users_path,
        UserEntry(id="alice", port=8030, enabled=True),
        UserEntry(id="bob", port=8031, enabled=False),
    )
    sup.alive.add("alice")  # bob is disabled, no worker
    (tmp_path / "palaces" / "alice").mkdir(parents=True)

    views = admin.list_users()
    by_id = {v["spec"]["user_id"]: v for v in views}
    assert by_id["alice"]["health"]["worker_running"] is True
    assert by_id["alice"]["health"]["palace_initialized"] is True
    assert by_id["bob"]["health"]["worker_running"] is False
    # Disabled user has the note explaining why worker_running is false.
    assert "disabled" in by_id["bob"]["health"]["note"]


async def test_get_user_missing_raises(admin_env) -> None:
    admin, _, _ = admin_env
    with pytest.raises(UserNotFound):
        admin.get_user("ghost")
