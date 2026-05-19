"""users.yaml schema validation (D1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eidolon.memory.config.users import (
    UsersConfig,
    bundled_users_template_path,
    ensure_users_yaml_exists,
    load_users_config,
)


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_load_users_yaml_basic(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "users": [
                {"id": "alice", "port": 8030},
                {"id": "bob", "port": 8031, "enabled": False},
            ]
        },
    )
    cfg = load_users_config(path=p)
    assert len(cfg.users) == 2
    assert {u.id for u in cfg.enabled_users()} == {"alice"}
    assert cfg.find("bob").enabled is False


def test_load_users_yaml_missing_file_returns_empty(tmp_path: Path) -> None:
    cfg = load_users_config(path=tmp_path / "nope.yaml")
    assert cfg.users == []


def test_duplicate_user_id_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "users": [
                {"id": "alice", "port": 8030},
                {"id": "alice", "port": 8031},
            ]
        },
    )
    with pytest.raises(ValueError, match="duplicate user id"):
        load_users_config(path=p)


def test_enabled_port_collision_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "users": [
                {"id": "alice", "port": 8030, "enabled": True},
                {"id": "bob", "port": 8030, "enabled": True},
            ]
        },
    )
    with pytest.raises(ValueError, match="port .* collision"):
        load_users_config(path=p)


def test_disabled_users_skip_port_collision(tmp_path: Path) -> None:
    """Two users may share a port if one is disabled (palace preserved but not running)."""
    p = _write(
        tmp_path,
        {
            "users": [
                {"id": "alice", "port": 8030, "enabled": True},
                {"id": "bob", "port": 8030, "enabled": False},
            ]
        },
    )
    cfg = load_users_config(path=p)
    assert len(cfg.users) == 2


def test_invalid_user_id_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, {"users": [{"id": "../escape", "port": 8030}]})
    with pytest.raises(ValueError):
        load_users_config(path=p)


def test_users_config_round_trip() -> None:
    cfg = UsersConfig.model_validate(
        {
            "users": [
                {"id": "alice", "port": 8030, "enabled": True, "palace_path": "/tmp/a"},
            ]
        }
    )
    assert cfg.users[0].palace_path == "/tmp/a"


def test_bundled_users_template_parses() -> None:
    """Seed template must round-trip through UsersConfig validation."""
    tpl = bundled_users_template_path()
    assert tpl.is_file(), f"bundled template missing: {tpl}"
    cfg = load_users_config(path=tpl)
    enabled = cfg.enabled_users()
    assert enabled, "bundled .tpl must declare at least one enabled user"
    assert any(u.id == "default" for u in enabled)


def test_ensure_users_yaml_exists_seeds_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "users.yaml"
    assert not target.exists()
    created = ensure_users_yaml_exists(target)
    assert created is True
    assert target.is_file()
    # The seeded file must validate.
    cfg = load_users_config(path=target)
    assert any(u.id == "default" for u in cfg.enabled_users())


def test_ensure_users_yaml_exists_preserves_existing(tmp_path: Path) -> None:
    target = tmp_path / "users.yaml"
    target.write_text("users:\n  - id: keepme\n    port: 9999\n", encoding="utf-8")
    created = ensure_users_yaml_exists(target)
    assert created is False
    cfg = load_users_config(path=target)
    assert {u.id for u in cfg.users} == {"keepme"}


def test_ensure_users_yaml_exists_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "users.yaml"
    assert not target.parent.exists()
    created = ensure_users_yaml_exists(target)
    assert created is True
    assert target.is_file()
