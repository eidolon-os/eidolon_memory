"""System Data Memory runtime roster contract parsing."""

from __future__ import annotations

import json
from io import BytesIO

import pytest
from eidolon_memory_contracts import (
    DEFAULT_MEMORY_MCP_BASE_PORT,
    MEMORY_MCP_PORT_SPAN,
    stable_memory_realm_port,
)

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.registry import load_users_config
from eidolon.memory.config.users import UsersConfig


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _settings() -> MemorySettings:
    return MemorySettings.model_validate(
        {
            "mcp_http": {
                "host": "127.0.0.1",
                "port": DEFAULT_MEMORY_MCP_BASE_PORT,
                "path": "/mcp",
            },
        }
    )


def _urlopen_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    *,
    expected_token: str = "memory-roster-token-00000001",
) -> None:
    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        del timeout
        assert request.full_url == (
            "http://127.0.0.1:8084/api/companion-authority/v1/memory-runtime-roster"
        )
        assert request.get_header("Authorization") == f"Bearer {expected_token}"
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("eidolon.memory.config.users.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN", expected_token)


def test_load_memory_realms_from_system_data(monkeypatch: pytest.MonkeyPatch) -> None:
    _urlopen_payload(
        monkeypatch,
        {
            "contract_version": "1",
            "operation": "memory.runtime-roster",
            "realms": [
                {
                    "realm_id": "r:benchmark:default",
                    "owner_id": "benchmark",
                    "companion_id": "test",
                    "engine": "mempalace",
                    "engine_config": {
                        "consolidator": {"enabled": True, "interval_hours": 8},
                    },
                }
            ],
        },
    )

    cfg = load_users_config(_settings())
    assert [u.id for u in cfg.users] == ["r:benchmark:default"]
    default = cfg.find("r:benchmark:default")
    assert default is not None
    assert default.owner_id == "benchmark"
    assert default.companion_id == "test"
    assert default.port == stable_memory_realm_port(
        "r:benchmark:default",
        base_port=DEFAULT_MEMORY_MCP_BASE_PORT,
        used_ports=set(),
    )
    assert default.enabled is True
    assert default.consolidator is not None
    assert default.consolidator.enabled is True
    assert default.consolidator.interval_hours == 8
    assert {u.id for u in cfg.enabled_users()} == {"r:benchmark:default"}


def test_load_memory_realms_assigns_stable_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "contract_version": "1",
        "operation": "memory.runtime-roster",
        "realms": [
                {
                    "realm_id": "r:benchmark:one",
                    "owner_id": "benchmark",
                    "companion_id": "one",
                    "engine": "mempalace",
                    "engine_config": {},
                },
                {
                    "realm_id": "r:benchmark:two",
                    "owner_id": "benchmark",
                    "companion_id": "two",
                    "engine": "mempalace",
                    "engine_config": {},
                },
            ],
    }
    _urlopen_payload(monkeypatch, payload)

    first = load_users_config(_settings())
    second = load_users_config(_settings())
    assert [(u.id, u.port) for u in first.users] == [(u.id, u.port) for u in second.users]
    assert len({u.port for u in first.users}) == 2
    assert all(
        DEFAULT_MEMORY_MCP_BASE_PORT
        <= u.port
        < DEFAULT_MEMORY_MCP_BASE_PORT + MEMORY_MCP_PORT_SPAN
        for u in first.users
    )


def test_load_memory_realms_ignores_runtime_route_in_engine_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _urlopen_payload(
        monkeypatch,
        {
            "contract_version": "1",
            "operation": "memory.runtime-roster",
            "realms": [
                {
                    "realm_id": "r:benchmark:default",
                    "owner_id": "benchmark",
                    "companion_id": "test",
                    "engine": "mempalace",
                    "engine_config": {
                        "mcp_http_url": "http://127.0.0.1:8041/mcp",
                        "palace_path": "/tmp/legacy-palace",
                    }
                }
            ],
        },
    )

    cfg = load_users_config(_settings())
    assert cfg.find("r:benchmark:default").port == stable_memory_realm_port(
        "r:benchmark:default",
        base_port=DEFAULT_MEMORY_MCP_BASE_PORT,
        used_ports=set(),
    )


def test_system_data_registry_requires_its_service_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="service credential is unavailable"):
        load_users_config(_settings())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"contract_version": "2", "operation": "memory.runtime-roster", "realms": []},
        {"contract_version": "1", "operation": "wrong", "realms": []},
        {"contract_version": "1", "operation": "memory.runtime-roster", "realms": {}},
        {
            "contract_version": "1",
            "operation": "memory.runtime-roster",
            "realms": [{"realm_id": "incomplete"}],
        },
    ],
)
def test_system_data_registry_rejects_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
) -> None:
    _urlopen_payload(monkeypatch, payload)

    with pytest.raises(RuntimeError, match="System Data Memory roster"):
        load_users_config(_settings())


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
