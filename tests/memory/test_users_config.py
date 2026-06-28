"""Admin owner workspace memory realm config parsing."""

from __future__ import annotations

import json
from io import BytesIO

import pytest

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.users import UsersConfig, load_users_config


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _settings() -> MemorySettings:
    return MemorySettings.model_validate(
        {
            "wings": [{"id": "Wing_Life", "display_name": "life"}],
            "mcp_http": {"host": "127.0.0.1", "port": 8030, "path": "/mcp"},
        }
    )


def _urlopen_routes(monkeypatch: pytest.MonkeyPatch, routes: dict[str, dict]) -> None:
    def fake_urlopen(url, timeout=0):  # noqa: ANN001
        del timeout
        payload = routes.get(str(url))
        if payload is None:
            raise AssertionError(f"unexpected url: {url}")
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("eidolon.memory.config.users.urllib.request.urlopen", fake_urlopen)


def test_load_memory_realms_from_owner_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "http://127.0.0.1:9000"
    _urlopen_routes(
        monkeypatch,
        {
            f"{base}/api/owners": {
                "owners": [
                    {"owner_id": "benchmark", "status": "active"},
                    {"owner_id": "archived", "status": "archived"},
                ]
            },
            f"{base}/api/owners/benchmark/companions": {
                "companions": [
                    {"companion_id": "test", "status": "active"},
                    {"companion_id": "old", "status": "archived"},
                ]
            },
            f"{base}/api/owners/benchmark/memory-realms": {
                "memory_realms": [
                    {
                        "realm_id": "r:benchmark:default",
                        "owner_id": "benchmark",
                        "companion_id": "test",
                        "status": "active",
                        "engine_config_json": {
                            "mcp_port": 8035,
                            "palace_path": "/tmp/palace",
                            "consolidator": {"enabled": True, "interval_hours": 8},
                        },
                    },
                    {
                        "realm_id": "r:benchmark:old",
                        "owner_id": "benchmark",
                        "companion_id": "old",
                        "status": "active",
                        "engine_config_json": {"mcp_port": 8036},
                    },
                    {
                        "realm_id": "r:benchmark:orphan",
                        "owner_id": "benchmark",
                        "companion_id": "missing",
                        "status": "active",
                        "engine_config_json": {"mcp_port": 8037},
                    },
                ]
            },
        },
    )

    cfg = load_users_config(_settings())
    assert [u.id for u in cfg.users] == ["r:benchmark:default", "r:benchmark:old"]
    default = cfg.find("r:benchmark:default")
    assert default is not None
    assert default.owner_id == "benchmark"
    assert default.companion_id == "test"
    assert default.port == 8035
    assert default.enabled is True
    assert default.palace_path == "/tmp/palace"
    assert default.consolidator is not None
    assert default.consolidator.enabled is True
    assert default.consolidator.interval_hours == 8
    assert {u.id for u in cfg.enabled_users()} == {"r:benchmark:default"}
    assert cfg.find("r:benchmark:old").enabled is False


def test_load_memory_realms_assigns_stable_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "http://127.0.0.1:9000"
    payloads = {
        f"{base}/api/owners": {"owners": [{"owner_id": "benchmark", "status": "active"}]},
        f"{base}/api/owners/benchmark/companions": {
            "companions": [
                {"companion_id": "one", "status": "active"},
                {"companion_id": "two", "status": "active"},
            ]
        },
        f"{base}/api/owners/benchmark/memory-realms": {
            "memory_realms": [
                {
                    "realm_id": "r:benchmark:one",
                    "owner_id": "benchmark",
                    "companion_id": "one",
                    "status": "active",
                    "engine_config_json": {},
                },
                {
                    "realm_id": "r:benchmark:two",
                    "owner_id": "benchmark",
                    "companion_id": "two",
                    "status": "active",
                    "engine_config_json": {},
                },
            ]
        },
    }
    _urlopen_routes(monkeypatch, payloads)

    first = load_users_config(_settings())
    second = load_users_config(_settings())
    assert [(u.id, u.port) for u in first.users] == [
        (u.id, u.port) for u in second.users
    ]
    assert len({u.port for u in first.users}) == 2
    assert all(8030 <= u.port <= 10029 for u in first.users)


def test_load_memory_realms_reads_port_from_mcp_url(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "http://127.0.0.1:9000"
    _urlopen_routes(
        monkeypatch,
        {
            f"{base}/api/owners": {
                "owners": [{"owner_id": "benchmark", "status": "active"}]
            },
            f"{base}/api/owners/benchmark/companions": {
                "companions": [{"companion_id": "test", "status": "active"}]
            },
            f"{base}/api/owners/benchmark/memory-realms": {
                "memory_realms": [
                    {
                        "realm_id": "r:benchmark:default",
                        "owner_id": "benchmark",
                        "companion_id": "test",
                        "status": "active",
                        "engine_config_json": {
                            "mcp_http_url": "http://127.0.0.1:8041/mcp"
                        },
                    }
                ]
            },
        },
    )

    cfg = load_users_config(_settings())
    assert cfg.find("r:benchmark:default").port == 8041


def test_duplicate_user_id_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate realm id"):
        UsersConfig.model_validate(
            {
                "users": [
                    {"id": "r:benchmark:default", "port": 8030},
                    {"id": "r:benchmark:default", "port": 8031},
                ]
            }
        )


def test_enabled_port_collision_rejected() -> None:
    with pytest.raises(ValueError, match="port .* collision"):
        UsersConfig.model_validate(
            {
                "users": [
                    {"id": "r:benchmark:default", "port": 8030, "enabled": True},
                    {"id": "r:benchmark:study", "port": 8030, "enabled": True},
                ]
            }
        )


def test_disabled_users_skip_port_collision() -> None:
    cfg = UsersConfig.model_validate(
        {
            "users": [
                {"id": "r:benchmark:default", "port": 8030, "enabled": True},
                {"id": "r:benchmark:study", "port": 8030, "enabled": False},
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
                    "id": "r:benchmark:default",
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
    ).find("r:benchmark:default")
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
