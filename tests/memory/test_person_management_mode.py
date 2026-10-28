from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from eidolon.memory.application.user_admin import UserAdmin, UserStillRegistered
from eidolon.memory.config.palace_directory import LEDGERS_DIR_SUFFIX
from eidolon.memory.config.users import UserEntry, UsersConfig
from eidolon.memory.entrypoints.agent_runner import ManagementOnlyApp
from eidolon.memory.entrypoints.supervisor import _agent_cli_argv


async def test_paused_runner_has_no_runtime_or_ops_mcp_surface():
    async def handle(request):
        return JSONResponse({"ok": True})

    app = ManagementOnlyApp(
        Starlette(
            routes=[
                Route("/api/memory/v1/status", handle),
                Route("/mcp", handle),
                Route("/ops/mcp", handle),
            ]
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/memory/v1/status")).status_code == 200
        assert (await client.get("/mcp")).status_code == 403
        assert (await client.get("/ops/mcp")).status_code == 403


def test_paused_person_serves_management_but_not_runtime(tmp_path):
    entry = UserEntry(
        id="r_person",
        owner_id="o",
        person_id="p",
        scope="person",
        port=12345,
        enabled=False,
        management_enabled=True,
    )
    config = UsersConfig(users=[entry])
    assert config.enabled_users() == []
    assert config.serving_users() == [entry]
    assert "--management-only" in _agent_cli_argv(entry, tmp_path)


async def test_person_erasure_clears_palace_ledgers_and_queue_only_with_intent(
    tmp_path, monkeypatch
):
    from eidolon.memory.application import user_admin as module
    from eidolon.memory.infrastructure import realm_erasure

    entry = UserEntry(
        id="r_person",
        owner_id="o",
        person_id="p",
        scope="person",
        port=12345,
        enabled=False,
        management_enabled=True,
    )
    monkeypatch.setattr(module, "load_users_config", lambda: UsersConfig(users=[entry]))
    settings = SimpleNamespace(
        nats=SimpleNamespace(url="nats://test", stream="MEMORY", durable_prefix="test")
    )
    monkeypatch.setattr(module, "get_memory_settings", lambda: settings)
    monkeypatch.setattr(module, "resolve_run_dir", lambda _: tmp_path / "run")
    purge = AsyncMock()
    monkeypatch.setattr(realm_erasure, "purge_realm_messages", purge)
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "data").write_text("private")
    ledger = tmp_path / ("palace" + LEDGERS_DIR_SUFFIX)
    ledger.mkdir()
    (ledger / "ledger").write_text("private")

    @asynccontextmanager
    async def paused(_):
        yield

    sup = SimpleNamespace(palace_path_for=lambda _: palace, realm_paused=paused)
    admin = UserAdmin(sup, trash_root=tmp_path / "trash")
    with pytest.raises(UserStillRegistered):
        await admin.erase_person_memory(entry.id, owner_id="o", person_id="p")
    assert (palace / "data").exists()
    purge.assert_not_called()
    entry.management_enabled = False
    with pytest.raises(UserStillRegistered):
        await admin.erase_person_memory(entry.id, owner_id="o", person_id="p")
    entry.erasure_pending = True
    await admin.erase_person_memory(entry.id, owner_id="o", person_id="p")
    assert not palace.exists() and not ledger.exists()
    purge.assert_awaited_once_with("nats://test", "MEMORY", "test", ["r_person"])
    await admin.erase_person_memory(entry.id, owner_id="o", person_id="p")
    assert not palace.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "kg_add_triple", "subject": "A", "predicate": "likes", "object": "tea"},
        {
            "kind": "privacy_mutation",
            "issuer": "agent",
            "action": "delete",
            "drawer_ids": ["drawer_test"],
            "preview_id": "p",
            "target": "tea",
        },
    ],
)
async def test_paused_worker_rejects_non_management_commands(tmp_path, payload):
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger
    from tests.memory.test_kg_command_flow import SPACE, _stub_msg

    ledger = CommandStatusLedger(tmp_path / "commands.sqlite3", space_id=SPACE)
    msg = _stub_msg(
        {
            **payload,
            "request_id": "blocked",
            "memory_space_id": SPACE,
            "issued_at": "2026-10-28T00:00:00Z",
        }
    )
    backend = AsyncMock()
    kg = AsyncMock()
    await process_command_message(
        msg,
        management_only=True,
        backend=backend,
        kg=kg,
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
        command_status=ledger,
    )
    assert msg.ack_calls == ["ack"] and not msg.nak_calls
    assert (await ledger.get("blocked")).status == "failed"
    assert not backend.mock_calls and not kg.mock_calls


async def test_maintenance_failure_releases_exclusion_without_losing_child(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from tests.memory.test_supervisor_roster_refresh import _supervisor

    sup = _supervisor(tmp_path, refresh_seconds=60)
    entry = UserEntry(id="r_person", port=12345)
    child = SimpleNamespace(terminate=Mock(side_effect=RuntimeError("stop failed")))
    sup._children[entry.id] = child
    monkeypatch.setattr(sup, "_terminate_consolidator", lambda _: None)
    reconcile = AsyncMock()
    monkeypatch.setattr(sup, "_reconcile", reconcile)
    with pytest.raises(RuntimeError, match="stop failed"):
        async with sup.realm_paused(entry):
            pytest.fail("maintenance must not run after termination failure")
    assert entry.id not in sup._maintenance_realms
    assert sup._children[entry.id] is child
    reconcile.assert_awaited_once()


async def test_reconcile_started_before_maintenance_cannot_spawn_inside_it(tmp_path, monkeypatch):
    import asyncio

    from eidolon.memory.entrypoints import supervisor as module
    from tests.memory.test_supervisor_roster_refresh import _supervisor

    sup = _supervisor(tmp_path, refresh_seconds=60)
    sup._eager_init = True
    entry = UserEntry(id="r_person", port=12345)
    monkeypatch.setattr(sup, "_read_users", lambda: UsersConfig(users=[entry]))
    entered, release = asyncio.Event(), asyncio.Event()

    async def init(_):
        entered.set()
        await release.wait()
        return entry.id, None

    monkeypatch.setattr(sup, "_init_user", init)

    def forbid_spawn(*args, **kwargs):
        pytest.fail("reconcile revived a realm during maintenance")

    monkeypatch.setattr(module, "_Child", forbid_spawn)
    task = asyncio.create_task(sup._reconcile())
    await entered.wait()
    async with sup.realm_paused(entry):
        release.set()
        await task
        assert entry.id not in sup._children
        monkeypatch.setattr(sup, "_reconcile", AsyncMock())


async def test_erasure_retry_clears_ledgers_even_after_palace_disappeared(tmp_path):
    from eidolon.memory.infrastructure.history_reset import clear_palace_contents

    palace = tmp_path / "missing"
    ledger = tmp_path / ("missing" + LEDGERS_DIR_SUFFIX)
    ledger.mkdir()
    (ledger / "pending-command.sqlite3").write_text("retained private content")
    assert clear_palace_contents(palace, allow_missing=True) == 1
    assert list(ledger.iterdir()) == []
    assert clear_palace_contents(palace, allow_missing=True) == 0


def test_provisional_worker_uses_management_mode_and_no_broad_runtime(tmp_path):
    entry = UserEntry(
        id="r_candidate",
        owner_id="o",
        person_id="p",
        scope="person",
        port=12345,
        enabled=False,
        management_enabled=True,
        provisional_ingest_enabled=True,
    )
    args = _agent_cli_argv(entry, tmp_path)
    assert "--management-only" in args and "--provisional-ingest" in args
    assert UsersConfig(users=[entry]).enabled_users() == []
    with pytest.raises(ValueError):
        UserEntry(id="r_owner", owner_id="o", port=12346, provisional_ingest_enabled=True)


@pytest.mark.parametrize("permission", ["provisional_ingest_enabled", "provisional_recall_enabled"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_provisional_ingest_policy_change_reconciles_worker(
    tmp_path, monkeypatch, enabled, permission
):
    from unittest.mock import Mock

    from tests.memory.test_supervisor_roster_refresh import _supervisor

    sup = _supervisor(tmp_path, refresh_seconds=60)
    old = UserEntry(
        id="r_candidate",
        owner_id="o",
        person_id="p",
        scope="person",
        port=12345,
        enabled=False,
        management_enabled=True,
        **{permission: not enabled},
    )
    new = old.model_copy(update={permission: enabled})
    child = SimpleNamespace(user=old, terminate=Mock())
    sup._children[old.id] = child
    monkeypatch.setattr(sup, "_read_users", lambda: UsersConfig(users=[new]))
    spawn = Mock()
    monkeypatch.setattr(sup, "_ensure_agent_child", spawn)
    monkeypatch.setattr(sup, "_terminate_consolidator", lambda _: None)
    await sup._reconcile()
    child.terminate.assert_called_once()
    spawn.assert_called_once_with(new)


def test_provisional_read_permission_does_not_enable_ingest_or_normal_mcp(tmp_path):
    entry = UserEntry(
        id="r_candidate",
        owner_id="o",
        person_id="p",
        scope="person",
        port=12345,
        enabled=False,
        management_enabled=True,
        provisional_recall_enabled=True,
    )
    args = _agent_cli_argv(entry, tmp_path)
    assert "--management-only" in args and "--provisional-recall" in args
    assert "--provisional-ingest" not in args
