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

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eidolon.memory.application import user_admin as user_admin_mod
from eidolon.memory.application.user_admin import UserAdmin
from eidolon.memory.config.users import UserEntry, UsersConfig
from eidolon.memory.entrypoints.admin_api import build_admin_api


class _StubSupervisor:
    """Same as in test_user_admin.py — duplicated for module isolation."""

    def __init__(self, palaces_root: Path) -> None:
        self._palaces_root = palaces_root
        self.registry = UsersConfig(users=[])
        self.alive: set[str] = set()
        self.rebuild_calls: list[tuple[str, Path]] = []

    async def reconcile_now(self) -> None:
        cfg = self.registry
        enabled_ids = {u.id for u in cfg.users if u.enabled}
        self.alive |= enabled_ids
        self.alive -= {u for u in list(self.alive) if u not in enabled_ids}

    def is_worker_alive(self, user_id: str) -> bool:
        return user_id in self.alive

    def palace_path_for(self, user: UserEntry) -> Path:
        return self._palaces_root / user.id

    async def rebuild_memory_index(self, user: UserEntry, *, log_path: Path) -> dict:
        self.rebuild_calls.append((user.id, log_path))
        await asyncio.sleep(0)
        return {
            "user_id": user.id,
            "palace_path": str(self.palace_path_for(user)),
            "backend": "chroma",
            "returncode": 0,
            "log_path": str(log_path),
        }


def _set_users(client: TestClient, *entries: UserEntry) -> None:
    client.supervisor.registry = UsersConfig(users=list(entries))  # type: ignore[attr-defined]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    sup = _StubSupervisor(tmp_path / "palaces")
    monkeypatch.setattr(user_admin_mod, "load_users_config", lambda: sup.registry)
    admin = UserAdmin(
        sup,
        trash_root=tmp_path / "trash",
        maintenance_log_root=tmp_path / "maintenance",
    )
    app = build_admin_api(admin)
    test_client = TestClient(app)
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
    _set_users(client, UserEntry(id="alice", port=8030, enabled=True))
    r = client.post("/api/admin/reconcile")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert "alice" in client.supervisor.alive  # type: ignore[attr-defined]


def test_rebuild_index_starts_async_job(client: TestClient) -> None:
    _set_users(client, UserEntry(id="alice", port=8030, enabled=True))

    r = client.post("/api/admin/users/alice/memory/rebuild-index")
    assert r.status_code == 202
    body = r.json()
    assert body["user_id"] == "alice"
    assert body["status"] in {"pending", "running", "succeeded"}
    assert body["log_path"].endswith(".log")

    status = client.get(f"/api/admin/memory/rebuild-index/{body['job_id']}")
    assert status.status_code == 200
    assert status.json()["user_id"] == "alice"


def test_rebuild_index_missing_user_returns_404(client: TestClient) -> None:
    r = client.post("/api/admin/users/ghost/memory/rebuild-index")
    assert r.status_code == 404


def test_rebuild_index_missing_job_returns_404(client: TestClient) -> None:
    r = client.get("/api/admin/memory/rebuild-index/nope")
    assert r.status_code == 404


def test_create_rejects_bad_user_id(client: TestClient) -> None:
    """validate_user_id (memory's regex) rejects spaces, slashes, non-ASCII,
    and ids that don't start with alnum. Dots ARE allowed (palace path
    separator semantics are handled elsewhere)."""
    for bad_id in ["bad id", "with/slash", "中文", "-leading-hyphen", "_leading_underscore"]:
        r = client.post("/api/admin/users", json={"user_id": bad_id})
        assert r.status_code == 422, f"expected 422 for {bad_id!r}, got {r.status_code}"


def test_create_duplicate_returns_409(client: TestClient) -> None:
    _set_users(client, UserEntry(id="alice", port=8030, enabled=True))
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
    _set_users(client, UserEntry(id="alice", port=8030, enabled=False))
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
    _set_users(
        client,
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
