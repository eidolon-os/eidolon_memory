"""T1-D: build_control_plane_mcp registers all KG tools when kg + publisher given."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SPACE = "default.alice.default"


@pytest.fixture
def mcp_with_kg(tmp_path: Path):
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.locked_backend import LockedBackend
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger

    settings = load_memory_settings()
    backend = LockedBackend(FakeMemoryBackend())
    kg_db = tmp_path / "kg.sqlite3"
    locked_kg = LockedKnowledgeGraph(KnowledgeGraph(db_path=str(kg_db)), backend.lock)
    publisher = AsyncMock()
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")

    mcp = build_control_plane_mcp(
        backend,
        settings,
        memory_space_id=SPACE,
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9999,
        kg=locked_kg,
        command_publisher=publisher,
        command_status=ledger,
    )
    yield mcp, locked_kg, publisher, ledger, backend
    locked_kg.close()


async def test_kg_tools_all_registered(mcp_with_kg) -> None:
    mcp, _, _, _, _ = mcp_with_kg
    names = {t.name for t in mcp._tool_manager.list_tools()}
    expected = {
        "eidolon_memory_kg_add_triple",
        "eidolon_memory_kg_invalidate",
        "eidolon_memory_kg_query_entity",
        "eidolon_memory_kg_timeline",
        "eidolon_memory_kg_stats",
        "eidolon_memory_kg_predicates",
        "eidolon_memory_command_status",
        "eidolon_memory_forget_preview",
        "eidolon_memory_forget_confirm",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"


async def test_privacy_preview_is_read_only_and_confirm_publishes_exact_ids(
    mcp_with_kg,
) -> None:
    from eidolon.memory.domain.wire import MemoryWireRecord

    mcp, _, publisher, ledger, backend = mcp_with_kg
    for key, text in (
        ("drawer_tea_1", "以前喜欢绿茶"),
        ("drawer_tea_2", "现在不买绿茶"),
    ):
        backend.inner.docs[f"{SPACE}::{key}"] = MemoryWireRecord(
            memory_space_id=SPACE,
            key=key,
            value=text,
            metadata={"memory_space_id": SPACE, "wing": "Wing_Profile"},
        )
    preview_tool = next(
        tool for tool in mcp._tool_manager.list_tools()
        if tool.name == "eidolon_memory_forget_preview"
    )
    confirm_tool = next(
        tool for tool in mcp._tool_manager.list_tools()
        if tool.name == "eidolon_memory_forget_confirm"
    )

    preview = await preview_tool.fn(target="绿茶", action="delete")

    assert preview["status"] == "preview"
    assert preview["requires_explicit_confirmation"] is True
    assert [row["drawer_id"] for row in preview["candidates"]] == [
        "drawer_tea_1",
        "drawer_tea_2",
    ]
    assert publisher.publish.await_count == 0
    assert await backend.get(SPACE, "drawer_tea_1") is not None

    async def _record_applied(command):
        await ledger.record_applied(
            command.request_id,
            kind=command.kind,
            resource_id=f"{command.action}:2:{command.preview_id}",
        )

    publisher.publish.side_effect = _record_applied
    result = await confirm_tool.fn(
        confirmation_token=preview["confirmation_token"],
        wait_applied_seconds=0.1,
    )

    assert result["status"] == "applied"
    command = publisher.publish.await_args.args[0]
    assert command.kind == "privacy_mutation"
    assert command.drawer_ids == ["drawer_tea_1", "drawer_tea_2"]
    assert command.memory_space_id == SPACE


async def test_privacy_confirm_rejects_tampered_preview(mcp_with_kg) -> None:
    mcp, _, publisher, _, _ = mcp_with_kg
    confirm_tool = next(
        tool for tool in mcp._tool_manager.list_tools()
        if tool.name == "eidolon_memory_forget_confirm"
    )

    result = await confirm_tool.fn(confirmation_token="forged.token")

    assert result["status"] == "error"
    assert publisher.publish.await_count == 0


async def test_kg_tools_omitted_without_publisher(tmp_path: Path) -> None:
    """build_control_plane_mcp without kg/publisher must not register KG tools."""
    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp

    settings = load_memory_settings()
    mcp = build_control_plane_mcp(
        FakeMemoryBackend(),
        settings,
        memory_space_id="default.alice.default",
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9999,
    )
    names = {t.name for t in mcp._tool_manager.list_tools()}
    kg_tools = {n for n in names if "_kg_" in n}
    assert not kg_tools


async def test_kg_predicates_tool_returns_whitelist(mcp_with_kg) -> None:
    mcp, _, _, _, _ = mcp_with_kg
    tool = next(
        t for t in mcp._tool_manager.list_tools()
        if t.name == "eidolon_memory_kg_predicates"
    )
    result = await tool.fn()
    assert "likes" in result["predicates"]
    assert "has_health_condition" in result["sensitive"]
    assert result["count"] >= 27


async def test_kg_add_triple_publishes_then_reads_status(mcp_with_kg) -> None:
    mcp, locked_kg, publisher, ledger, _ = mcp_with_kg
    add_tool = next(
        t for t in mcp._tool_manager.list_tools() if t.name == "eidolon_memory_kg_add_triple"
    )

    # Simulate worker applying immediately after publish. The worker wins the
    # accepted/applied race, which must never downgrade the final status.
    async def _apply_then_publish(cmd):
        # The publisher mock IS the worker stand-in here: it commits the triple
        # via locked_kg directly, then returns.
        triple_id = await locked_kg.add_triple(
            subject=cmd.subject, predicate=cmd.predicate, object=cmd.object,
            valid_from=cmd.valid_from, valid_to=cmd.valid_to,
            confidence=cmd.confidence,
            source_turn_id=cmd.source_drawer_id or f"req:{cmd.request_id}",
            adapter_name=cmd.adapter_name,
        )
        await ledger.record_applied(
            cmd.request_id,
            kind=cmd.kind,
            resource_id=triple_id,
        )

    publisher.publish.side_effect = _apply_then_publish

    result = await add_tool.fn(
        subject="self",
        predicate="likes",
        object="tea",
        wait_visible_seconds=2.0,
    )
    assert result["status"] == "applied"
    assert result["triple_id"]
    publisher.publish.assert_awaited_once()


async def test_kg_add_triple_returns_accepted_when_worker_silent(mcp_with_kg) -> None:
    """Durably published is accepted, never falsely reported as applied."""
    mcp, _, publisher, _, _ = mcp_with_kg

    async def _noop(cmd):
        return None

    publisher.publish.side_effect = _noop
    add_tool = next(
        t for t in mcp._tool_manager.list_tools() if t.name == "eidolon_memory_kg_add_triple"
    )
    result = await add_tool.fn(
        subject="self",
        predicate="likes",
        object="never_applied",
        wait_visible_seconds=0.1,   # short for the test
    )
    assert result["status"] == "accepted"
    assert result["triple_id"] is None


async def test_kg_query_entity_excludes_sensitive_by_default(mcp_with_kg) -> None:
    mcp, locked_kg, _, _, _ = mcp_with_kg
    await locked_kg.add_triple(
        subject="self", predicate="has_health_condition", object="anxiety",
        source_turn_id="seed", adapter_name="test",
    )
    await locked_kg.add_triple(
        subject="self", predicate="likes", object="tea",
        source_turn_id="seed2", adapter_name="test",
    )
    query_tool = next(
        t for t in mcp._tool_manager.list_tools() if t.name == "eidolon_memory_kg_query_entity"
    )
    default = await query_tool.fn(name="self")
    preds_default = {t["predicate"] for t in default["triples"]}
    assert "likes" in preds_default
    assert "has_health_condition" not in preds_default

    opt_in = await query_tool.fn(name="self", include_sensitive=True)
    preds_opt = {t["predicate"] for t in opt_in["triples"]}
    assert "has_health_condition" in preds_opt


async def test_command_status_read_does_not_wait_for_backend_lock(mcp_with_kg) -> None:
    """The asynchronous-write projection is physically separate from Chroma/KG."""
    mcp, _, _, ledger, backend = mcp_with_kg
    await ledger.record_applied(
        "status-fast",
        kind="memory_intent",
        resource_id="memoryintent:intent:status-fast",
    )
    status_tool = next(
        t
        for t in mcp._tool_manager.list_tools()
        if t.name == "eidolon_memory_command_status"
    )

    async with backend.lock:
        result = await asyncio.wait_for(
            status_tool.fn(request_id="status-fast"),
            timeout=0.2,
        )

    assert result["status"] == "applied"
    assert result["resource_id"] == "memoryintent:intent:status-fast"


async def test_command_status_stats_tool_reads_projection_capacity(mcp_with_kg) -> None:
    mcp, _, _, ledger, _ = mcp_with_kg
    await ledger.record_accepted("active", kind="privacy_mutation")
    tool = next(
        item for item in mcp._tool_manager.list_tools()
        if item.name == "eidolon_memory_command_status_stats"
    )

    result = await tool.fn()

    assert result["total"] >= 1
    assert result["accepted"] >= 1
    assert result["max_records"] == 100_000


async def test_dlq_mcp_list_detail_replay_resolve(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger
    from eidolon.memory.infrastructure.dlq import DlqLedger

    publisher = AsyncMock()
    dlq = DlqLedger(tmp_path / "dlq.sqlite3")
    mcp = build_control_plane_mcp(
        FakeMemoryBackend(),
        load_memory_settings(),
        memory_space_id=SPACE,
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9999,
        command_publisher=publisher,
        command_status=CommandStatusLedger(tmp_path / "status.sqlite3"),
        dlq_store=dlq,
        replay_publisher=publisher,
    )
    first = await dlq.add(
        subject="eidolon.memory.cmd.test",
        payload=b'{"secret":"full payload"}',
        error="boom",
        deliveries=3,
    )
    second = await dlq.add(
        subject="eidolon.memory.turn.test",
        payload=b"turn",
        error="bad turn",
        deliveries=3,
    )
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    listed = await tools["eidolon_memory_dlq_list"].fn()
    detail = await tools["eidolon_memory_dlq_detail"].fn(entry_id=first.entry_id)
    replayed = await tools["eidolon_memory_dlq_replay"].fn(entry_id=first.entry_id)
    duplicate = await tools["eidolon_memory_dlq_replay"].fn(entry_id=first.entry_id)
    resolved = await tools["eidolon_memory_dlq_resolve"].fn(
        entry_id=second.entry_id,
        note="legacy invalid payload",
    )

    assert listed["stats"]["unresolved"] == 2
    assert len(listed["records"]) == 2
    assert "payload" not in detail["record"]
    assert replayed["status"] == "replayed"
    publisher.replay_raw.assert_awaited_once_with(
        "eidolon.memory.cmd.test", b'{"secret":"full payload"}'
    )
    assert duplicate["status"] == "not_replayable"
    assert resolved["status"] == "resolved"


async def test_user_confirm_reports_accepted_not_applied_when_worker_is_silent(
    mcp_with_kg,
) -> None:
    mcp, _, publisher, ledger, backend = mcp_with_kg
    publisher.publish.side_effect = lambda _command: None
    tool = next(
        t for t in mcp._tool_manager.list_tools() if t.name == "eidolon_memory_user_confirm"
    )

    result = await tool.fn(text="我喜欢乌龙茶", wait_applied_seconds=0.01)

    assert result["status"] == "accepted"
    record = await ledger.get(result["request_id"])
    assert record is not None
    assert record.status == "accepted"
    assert backend.inner.docs == {}
    command = publisher.publish.await_args.args[0]
    assert command.kind == "memory_intent"
    assert command.intent.authority == "explicit_user"
    assert command.intent.raw_claim == "我喜欢乌龙茶"


async def test_user_confirm_publish_failure_is_truthfully_failed(mcp_with_kg) -> None:
    mcp, _, publisher, ledger, _ = mcp_with_kg

    async def _fail(_command):
        raise RuntimeError("NATS unavailable")

    publisher.publish.side_effect = _fail
    tool = next(
        t for t in mcp._tool_manager.list_tools() if t.name == "eidolon_memory_user_confirm"
    )

    result = await tool.fn(text="我喜欢乌龙茶", wait_applied_seconds=0.01)

    assert result["status"] == "failed"
    assert "NATS unavailable" in result["error"]
    record = await ledger.get(result["request_id"])
    assert record is not None
    assert record.status == "failed"
