"""T1-D: build_control_plane_mcp registers all KG tools when kg + publisher given."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mcp_with_kg(tmp_path: Path):
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.locked_backend import LockedBackend
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp

    settings = load_memory_settings()
    backend = LockedBackend(FakeMemoryBackend())
    kg_db = tmp_path / "kg.sqlite3"
    locked_kg = LockedKnowledgeGraph(KnowledgeGraph(db_path=str(kg_db)), backend.lock)
    publisher = AsyncMock()

    mcp = build_control_plane_mcp(
        backend,
        settings,
        user_id="alice",
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9999,
        kg=locked_kg,
        command_publisher=publisher,
    )
    yield mcp, locked_kg, publisher
    locked_kg.close()


async def test_kg_tools_all_registered(mcp_with_kg) -> None:
    mcp, _, _ = mcp_with_kg
    names = {t.name for t in mcp._tool_manager.list_tools()}
    expected = {
        "eidolon_memory_kg_add_triple",
        "eidolon_memory_kg_invalidate",
        "eidolon_memory_kg_query_entity",
        "eidolon_memory_kg_timeline",
        "eidolon_memory_kg_stats",
        "eidolon_memory_kg_predicates",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"


async def test_kg_tools_omitted_without_publisher(tmp_path: Path) -> None:
    """build_control_plane_mcp without kg/publisher must not register KG tools."""
    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp

    settings = load_memory_settings()
    mcp = build_control_plane_mcp(
        FakeMemoryBackend(),
        settings,
        user_id="alice",
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9999,
    )
    names = {t.name for t in mcp._tool_manager.list_tools()}
    kg_tools = {n for n in names if "_kg_" in n}
    assert not kg_tools


async def test_kg_predicates_tool_returns_whitelist(mcp_with_kg) -> None:
    mcp, _, _ = mcp_with_kg
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "eidolon_memory_kg_predicates")
    result = await tool.fn()
    assert "likes" in result["predicates"]
    assert "has_health_condition" in result["sensitive"]
    assert result["count"] >= 27


async def test_kg_add_triple_publishes_then_polls(mcp_with_kg) -> None:
    mcp, locked_kg, publisher = mcp_with_kg
    add_tool = next(
        t for t in mcp._tool_manager.list_tools() if t.name == "eidolon_memory_kg_add_triple"
    )

    # Simulate worker applying immediately after publish (race the polling loop).
    async def _apply_then_publish(cmd):
        # The publisher mock IS the worker stand-in here: it commits the triple
        # via locked_kg directly, then returns.
        await locked_kg.add_triple(
            subject=cmd.subject, predicate=cmd.predicate, object=cmd.object,
            valid_from=cmd.valid_from, valid_to=cmd.valid_to,
            confidence=cmd.confidence,
            source_turn_id=cmd.source_drawer_id or f"req:{cmd.request_id}",
            adapter_name=cmd.adapter_name,
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


async def test_kg_add_triple_returns_pending_when_publisher_silent(mcp_with_kg) -> None:
    """If the worker never applies, polling times out → pending status."""
    mcp, _, publisher = mcp_with_kg

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
    assert result["status"] == "pending"
    assert result["triple_id"] is None


async def test_kg_query_entity_excludes_sensitive_by_default(mcp_with_kg) -> None:
    mcp, locked_kg, _ = mcp_with_kg
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
