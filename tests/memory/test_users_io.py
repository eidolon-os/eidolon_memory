"""users.yaml atomic + cross-process-safe writer tests."""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest
import yaml

from eidolon.memory.config.users import UserEntry, load_users_config
from eidolon.memory.config.users_io import (
    UsersYamlError,
    remove_user,
    update_enabled,
    upsert_user,
)


def _seed(path: Path, users: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"users": users}, allow_unicode=True))


def test_upsert_appends_new_user(tmp_path: Path) -> None:
    p = tmp_path / "users.yaml"
    _seed(p, [{"id": "alice", "port": 8030, "enabled": True}])
    upsert_user(p, UserEntry(id="bob", port=8031, enabled=True))
    cfg = load_users_config(path=p)
    assert {u.id for u in cfg.users} == {"alice", "bob"}


def test_upsert_replaces_existing(tmp_path: Path) -> None:
    p = tmp_path / "users.yaml"
    _seed(p, [{"id": "alice", "port": 8030, "enabled": True}])
    upsert_user(p, UserEntry(id="alice", port=8099, enabled=True))
    cfg = load_users_config(path=p)
    assert len(cfg.users) == 1
    assert cfg.users[0].port == 8099


def test_upsert_rejects_port_collision(tmp_path: Path) -> None:
    p = tmp_path / "users.yaml"
    _seed(
        p,
        [
            {"id": "alice", "port": 8030, "enabled": True},
            {"id": "bob",   "port": 8031, "enabled": True},
        ],
    )
    with pytest.raises(UsersYamlError, match="port 8031 collision"):
        upsert_user(p, UserEntry(id="charlie", port=8031, enabled=True))


def test_update_enabled_flips_flag(tmp_path: Path) -> None:
    p = tmp_path / "users.yaml"
    _seed(p, [{"id": "alice", "port": 8030, "enabled": True}])
    update_enabled(p, "alice", False)
    cfg = load_users_config(path=p)
    assert cfg.users[0].enabled is False


def test_update_enabled_unknown_id(tmp_path: Path) -> None:
    p = tmp_path / "users.yaml"
    _seed(p, [{"id": "alice", "port": 8030, "enabled": True}])
    with pytest.raises(UsersYamlError, match="not found"):
        update_enabled(p, "nobody", True)


def test_remove_user(tmp_path: Path) -> None:
    p = tmp_path / "users.yaml"
    _seed(
        p,
        [
            {"id": "alice", "port": 8030, "enabled": True},
            {"id": "bob", "port": 8031, "enabled": True},
        ],
    )
    remove_user(p, "alice")
    cfg = load_users_config(path=p)
    assert [u.id for u in cfg.users] == ["bob"]


# ─── fcntl cross-process write serialization ────────────────────────────


def _child_writer(path_str: str, uid: str, port: int, hold_seconds: float) -> None:
    """Worker run in a subprocess: upsert one user, briefly hold the lock."""
    from eidolon.memory.config.users import UserEntry
    from eidolon.memory.config.users_io import upsert_user

    upsert_user(Path(path_str), UserEntry(id=uid, port=port, enabled=True))
    time.sleep(hold_seconds)


def test_concurrent_writers_dont_corrupt(tmp_path: Path) -> None:
    """Spawn 5 processes each adding their own user; final yaml must contain
    all 5 (no lost writes from non-atomic interleaving).
    """
    p = tmp_path / "users.yaml"
    _seed(p, [])

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_child_writer, args=(str(p), f"u{i}", 9000 + i, 0.0))
        for i in range(5)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0, f"writer {proc.pid} exit {proc.exitcode}"

    cfg = load_users_config(path=p)
    ids = sorted(u.id for u in cfg.users)
    assert ids == [f"u{i}" for i in range(5)], (
        f"expected u0..u4, got {ids} — concurrent writers lost data"
    )
