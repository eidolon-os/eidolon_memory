"""HTTP-layer tests for the supervisor's admin API.

These wrap a real ``UserAdmin`` (against a stub Supervisor + tmp_path
registry fixture) in FastAPI's ``TestClient`` so the request → handler →
orchestrator pipeline is exercised end-to-end without spawning real
subprocesses.

What this layer covers (in addition to test_user_admin.py):
  - HTTP status codes from UserAdminError subclasses (404/409/503)
  - Pydantic request validation (bad user_id chars, bad port range)
  - GET/reconcile/DELETE wiring + JSON envelope shapes
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from eidolon.memory.application.user_admin import UserAdmin
from eidolon.memory.config.users import UserEntry, UsersConfig
from eidolon.memory.entrypoints.admin_api import build_admin_api


class _StubSupervisor:
    """Same as in test_user_admin.py — duplicated for module isolation."""

    def __init__(self, users_path: Path, palaces_root: Path) -> None:
        self.users_path = users_path
        self._palaces_root = palaces_root
        self.alive: set[str] = set()

    async def reconcile_now(self) -> None:
        cfg = _read_users(self.users_path)
        enabled_ids = {u.id for u in cfg.users if u.enabled}
        self.alive |= enabled_ids
        self.alive -= {u for u in list(self.alive) if u not in enabled_ids}

    def is_worker_alive(self, user_id: str) -> bool:
        return user_id in self.alive

    def palace_path_for(self, user: UserEntry) -> Path:
        return self._palaces_root / user.id


def _read_users(path: Path) -> UsersConfig:
    if not path.is_file():
        return UsersConfig(users=[])
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return UsersConfig.model_validate(raw)


def _write_users(path: Path, *entries: UserEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"users": [u.model_dump(mode="json") for u in entries]}
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    users_yaml = tmp_path / "users.yaml"
    _write_users(users_yaml)
    sup = _StubSupervisor(users_yaml, tmp_path / "palaces")
    admin = UserAdmin(sup, trash_root=tmp_path / "trash")
    app = build_admin_api(admin)
    test_client = TestClient(app)
    test_client.users_path = users_yaml  # type: ignore[attr-defined]
    test_client.supervisor = sup  # type: ignore[attr-defined]
    return test_client


def test_health_endpoint(client: TestClient) -> None:
    r = client.get("/api/admin/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_list_users_empty(client: TestClient) -> None:
    r = client.get("/api/admin/users")
    assert r.status_code == 200
    assert r.json() == {"users": [], "memory_available": True}


def test_create_user_is_read_only(client: TestClient) -> None:
    r = client.post(
        "/api/admin/users",
        json={"user_id": "alice"},
    )
    assert r.status_code == 409
    assert "read-only" in r.json()["detail"]

    r2 = client.get("/api/admin/users")
    assert r2.status_code == 200
    assert r2.json()["users"] == []


def test_reconcile_starts_enabled_worker(client: TestClient) -> None:
    _write_users(
        client.users_path,  # type: ignore[attr-defined]
        UserEntry(id="alice", port=8030, enabled=True),
    )
    r = client.post("/api/admin/reconcile")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert "alice" in client.supervisor.alive  # type: ignore[attr-defined]


def test_create_rejects_bad_user_id(client: TestClient) -> None:
    """validate_user_id (memory's regex) rejects spaces, slashes, non-ASCII,
    and ids that don't start with alnum. Dots ARE allowed (palace path
    separator semantics are handled elsewhere)."""
    for bad_id in ["bad id", "with/slash", "中文", "-leading-hyphen", "_leading_underscore"]:
        r = client.post("/api/admin/users", json={"user_id": bad_id})
        assert r.status_code == 422, f"expected 422 for {bad_id!r}, got {r.status_code}"


def test_create_duplicate_returns_409(client: TestClient) -> None:
    _write_users(
        client.users_path,  # type: ignore[attr-defined]
        UserEntry(id="alice", port=8030, enabled=True),
    )
    r = client.post("/api/admin/users", json={"user_id": "alice"})
    assert r.status_code == 409
    assert "read-only" in r.json()["detail"]


def test_create_with_explicit_port_collision_returns_409(client: TestClient) -> None:
    r = client.post("/api/admin/users", json={"user_id": "bob", "port": 8030})
    assert r.status_code == 409
    assert "read-only" in r.json()["detail"]


def test_get_missing_user_returns_404(client: TestClient) -> None:
    r = client.get("/api/admin/users/ghost")
    assert r.status_code == 404


def test_delete_happy_path(client: TestClient) -> None:
    _write_users(
        client.users_path,  # type: ignore[attr-defined]
        UserEntry(id="alice", port=8030, enabled=False),
    )
    r = client.delete("/api/admin/users/alice")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True
    assert body["user_id"] == "alice"
    # palace_trashed_to is None because we never wrote a palace dir for the
    # synthetic alice — that's expected and not an error.
    # Registry ownership stays with admin; memory does not remove the row.
    r2 = client.get("/api/admin/users")
    assert [u["spec"]["user_id"] for u in r2.json()["users"]] == ["alice"]


def test_delete_missing_returns_404(client: TestClient) -> None:
    r = client.delete("/api/admin/users/ghost")
    assert r.status_code == 404


def test_list_with_consolidator_config(client: TestClient) -> None:
    """Consolidator config is read from the admin-owned registry."""
    _write_users(
        client.users_path,  # type: ignore[attr-defined]
        UserEntry(
            id="alice",
            port=8030,
            consolidator={
                "enabled": True,
                "interval_hours": 4.5,
                "window_days": 14,
                "min_drawers": 2,
                "min_confidence": 0.75,
            },
        ),
    )
    r = client.get("/api/admin/users")
    assert r.status_code == 200
    cons = r.json()["users"][0]["spec"]["consolidator"]
    assert cons["enabled"] is True
    assert cons["interval_hours"] == 4.5
    assert cons["window_days"] == 14
    assert cons["min_drawers"] == 2
    assert cons["min_confidence"] == 0.75
