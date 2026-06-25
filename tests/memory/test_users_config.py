"""Admin registry user config parsing."""

from __future__ import annotations

import json
from io import BytesIO

import pytest

from eidolon.memory.config.users import UsersConfig, load_users_config


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _urlopen_payload(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    def fake_urlopen(url, timeout=0):  # noqa: ANN001
        del url, timeout
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("eidolon.memory.config.users.urllib.request.urlopen", fake_urlopen)


def test_load_users_from_admin_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _urlopen_payload(
        monkeypatch,
        {
            "users": [
                {
                    "spec": {
                        "user_id": "alice",
                        "enabled": True,
                        "memory_port": 8030,
                        "palace_path": "",
                    }
                },
                {
                    "spec": {
                        "user_id": "bob",
                        "enabled": False,
                        "memory_port": 8031,
                    }
                },
            ]
        },
    )

    cfg = load_users_config()
    assert [u.id for u in cfg.users] == ["default.alice.default", "default.bob.default"]
    assert {u.id for u in cfg.enabled_users()} == {"default.alice.default"}
    assert cfg.find("default.bob.default").enabled is False


def test_load_users_falls_back_to_mcp_url_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _urlopen_payload(
        monkeypatch,
        {
            "users": [
                {
                    "spec": {"user_id": "alice", "enabled": True},
                    "mcp_http_url": "http://127.0.0.1:8030/mcp",
                }
            ]
        },
    )

    cfg = load_users_config()
    assert cfg.find("default.alice.default").port == 8030


def test_duplicate_user_id_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate user id"):
        UsersConfig.model_validate(
            {
                "users": [
                    {"id": "default.alice.default", "port": 8030},
                    {"id": "default.alice.default", "port": 8031},
                ]
            }
        )


def test_enabled_port_collision_rejected() -> None:
    with pytest.raises(ValueError, match="port .* collision"):
        UsersConfig.model_validate(
            {
                "users": [
                    {"id": "default.alice.default", "port": 8030, "enabled": True},
                    {"id": "default.bob.default", "port": 8030, "enabled": True},
                ]
            }
        )


def test_disabled_users_skip_port_collision() -> None:
    cfg = UsersConfig.model_validate(
        {
            "users": [
                {"id": "default.alice.default", "port": 8030, "enabled": True},
                {"id": "default.bob.default", "port": 8030, "enabled": False},
            ]
        }
    )
    assert len(cfg.users) == 2


def test_invalid_user_id_rejected() -> None:
    with pytest.raises(ValueError):
        UsersConfig.model_validate({"users": [{"id": "../escape", "port": 8030}]})


def test_user_consolidator_enabled_with_overrides() -> None:
    user = UsersConfig.model_validate(
        {
            "users": [
                {
                    "id": "default.alice.default",
                    "port": 8030,
                    "consolidator": {
                        "enabled": True,
                        "interval_hours": 12,
                        "window_days": 14,
                        "min_drawers": 5,
                        "min_confidence": 0.75,
                    },
                }
            ]
        }
    ).find("default.alice.default")
    assert user is not None
    assert user.consolidator_enabled() is True
    assert user.consolidator.interval_hours == 12
    assert user.consolidator.window_days == 14
    assert user.consolidator.min_drawers == 5
    assert user.consolidator.min_confidence == 0.75


def test_user_consolidator_rejects_invalid_values() -> None:
    import pydantic

    for bad in (
        {"enabled": True, "interval_hours": 0},
        {"enabled": True, "interval_hours": -1},
        {"enabled": True, "window_days": 0},
        {"enabled": True, "min_drawers": 0},
        {"enabled": True, "min_confidence": 1.5},
        {"enabled": True, "min_confidence": -0.1},
    ):
        with pytest.raises(pydantic.ValidationError):
            UsersConfig.model_validate(
                {"users": [{"id": "alice", "port": 8030, "consolidator": bad}]}
            )
