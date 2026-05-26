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


# ─── Phase 4 — consolidator config schema ─────────────────────────────────


def test_user_without_consolidator_block_disabled_by_default(tmp_path: Path) -> None:
    """Backwards-compat: existing users.yaml without ``consolidator:`` block."""
    p = _write(tmp_path, {"users": [{"id": "alice", "port": 8030}]})
    user = load_users_config(path=p).find("alice")
    assert user is not None
    assert user.consolidator is None
    assert user.consolidator_enabled() is False


def test_user_consolidator_explicit_disabled(tmp_path: Path) -> None:
    """``consolidator: {enabled: false}`` parses but stays off."""
    p = _write(tmp_path, {
        "users": [{
            "id": "alice", "port": 8030,
            "consolidator": {"enabled": False},
        }],
    })
    user = load_users_config(path=p).find("alice")
    assert user.consolidator is not None
    assert user.consolidator.enabled is False
    assert user.consolidator_enabled() is False


def test_user_consolidator_enabled_with_overrides(tmp_path: Path) -> None:
    """Per-user overrides flow into the pydantic model."""
    p = _write(tmp_path, {
        "users": [{
            "id": "alice", "port": 8030,
            "consolidator": {
                "enabled": True,
                "interval_hours": 12,
                "window_days": 14,
                "min_drawers": 5,
                "min_confidence": 0.75,
            },
        }],
    })
    user = load_users_config(path=p).find("alice")
    assert user.consolidator_enabled() is True
    assert user.consolidator.interval_hours == 12
    assert user.consolidator.window_days == 14
    assert user.consolidator.min_drawers == 5
    assert user.consolidator.min_confidence == 0.75


def test_user_consolidator_defaults_when_enabled_only(tmp_path: Path) -> None:
    """Only ``enabled: true`` is required — other knobs take defaults."""
    p = _write(tmp_path, {
        "users": [{
            "id": "alice", "port": 8030,
            "consolidator": {"enabled": True},
        }],
    })
    cfg = load_users_config(path=p).find("alice").consolidator
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.interval_hours == 6.0   # Phase 4 default
    assert cfg.window_days == 30
    assert cfg.min_drawers == 3
    assert cfg.min_confidence == 0.6


def test_user_consolidator_rejects_invalid_values(tmp_path: Path) -> None:
    """Pydantic must catch obviously-wrong knobs."""
    import pydantic
    for bad in (
        {"enabled": True, "interval_hours": 0},      # gt=0
        {"enabled": True, "interval_hours": -1},
        {"enabled": True, "window_days": 0},         # gt=0
        {"enabled": True, "min_drawers": 0},         # ge=1
        {"enabled": True, "min_confidence": 1.5},    # le=1
        {"enabled": True, "min_confidence": -0.1},   # ge=0
    ):
        p = _write(tmp_path, {
            "users": [{"id": "alice", "port": 8030, "consolidator": bad}]
        })
        with pytest.raises(pydantic.ValidationError):
            load_users_config(path=p)
