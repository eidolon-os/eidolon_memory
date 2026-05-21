"""Discovery HTTP contract for eidolon-agent routing."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from eidolon.memory.application import discovery
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.entrypoints import discovery_server

pytestmark = pytest.mark.asyncio


def _settings(users_path: Path) -> MemorySettings:
    return MemorySettings.model_validate(
        {
            "wings": [{"id": "Wing_Life", "display_name": "life"}],
            "supervisor": {"users_file": str(users_path)},
            "nats": {
                "url": "nats://127.0.0.1:4222",
                "stream": "MEMORY_TURNS",
                "conversation_turn_subject_base": "agent.memory.conversation.turn",
            },
            "mcp_http": {"host": "127.0.0.1", "port": 8030, "path": "/mcp"},
            "discovery_http": {
                "host": "127.0.0.1",
                "port": 8020,
                "path": "/api/discovery/agent-routing",
            },
        }
    )


def _write_users(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


async def test_discovery_returns_enabled_users_and_stable_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users_path = tmp_path / "users.yaml"
    _write_users(
        users_path,
        {
            "users": [
                {"id": "alice", "port": 8030, "enabled": True},
                {"id": "bob", "port": 8031, "enabled": False},
            ]
        },
    )

    async def fake_probe(url: str, *, timeout_seconds: float = 1.5) -> bool:
        del timeout_seconds
        return url == "http://127.0.0.1:8030/mcp"

    monkeypatch.setattr(discovery, "probe_mcp_http", fake_probe)

    payload = await discovery.build_agent_routing_discovery(_settings(users_path))

    assert payload["version"] == 1
    assert payload["nats"] == {
        "url": "nats://127.0.0.1:4222",
        "stream": "MEMORY_TURNS",
        "turn_subject_template": "agent.memory.conversation.turn.{user_id}",
        "cmd_subject_template": "agent.memory.cmd.{user_id}",
    }
    assert payload["users"] == [
        {
            "user_id": "alice",
            "enabled": True,
            "mcp_http_url": "http://127.0.0.1:8030/mcp",
            "mcp_auth": {"type": "none"},
            "agent_reachable": True,
        }
    ]
    raw = json.dumps(payload)
    for forbidden in ("users_yaml", "palace_path", "pid", "log_path"):
        assert forbidden not in raw


async def test_discovery_uses_default_user_when_users_yaml_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(url: str, *, timeout_seconds: float = 1.5) -> bool:
        del url, timeout_seconds
        return False

    monkeypatch.setattr(discovery, "probe_mcp_http", fake_probe)

    payload = await discovery.build_agent_routing_discovery(
        _settings(tmp_path / "missing-users.yaml")
    )

    assert payload["users"] == [
        {
            "user_id": "default",
            "enabled": True,
            "mcp_http_url": "http://127.0.0.1:8030/mcp",
            "mcp_auth": {"type": "none"},
            "agent_reachable": False,
        }
    ]


async def test_discovery_http_route_allows_no_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(url: str, *, timeout_seconds: float = 1.5) -> bool:
        del url, timeout_seconds
        return True

    monkeypatch.setattr(discovery, "probe_mcp_http", fake_probe)
    app = discovery_server.DiscoveryApp(_settings(tmp_path / "missing-users.yaml"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/discovery/agent-routing")

    assert res.status_code == 200
    payload = res.json()
    assert payload["users"][0]["mcp_auth"] == {"type": "none"}
    assert payload["users"][0]["agent_reachable"] is True


async def test_discovery_http_unknown_path_returns_404(tmp_path: Path) -> None:
    app = discovery_server.DiscoveryApp(_settings(tmp_path / "missing-users.yaml"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/users")

    assert res.status_code == 404
    assert res.json() == {"detail": "not found"}


async def test_discovery_http_500_does_not_leak_local_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(settings: MemorySettings) -> dict:
        del settings
        raise RuntimeError(f"broken config at {tmp_path}/users.yaml")

    monkeypatch.setattr(discovery_server, "build_agent_routing_discovery", boom)
    app = discovery_server.DiscoveryApp(_settings(tmp_path / "missing-users.yaml"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/discovery/agent-routing")

    assert res.status_code == 500
    assert res.json() == {"detail": "discovery unavailable"}
    assert str(tmp_path) not in res.text
