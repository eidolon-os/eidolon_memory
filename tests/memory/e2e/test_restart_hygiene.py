"""A real process restart preserves vector/KG/ledger state and pending writes."""

from __future__ import annotations

import pytest

from tests.memory.e2e.conftest import (
    e2e_actor_context,
    mcp_tool_json,
    nats_publish_assertion,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_recall_and_pending_commands_survive_same_space_restart(
    live_agent_runner,
    mcp_session,
):
    first = live_agent_runner(user_id="restart_state", steward_mode="noop")
    fact = "person:restart likes topic:oolong"
    request = await nats_publish_assertion(
        first.nats_url,
        user_id=first.user_id,
        text=fact,
        wing="Wing_Life",
        subject="person:restart",
        predicate="likes",
        object_value="topic:oolong",
    )

    async def snapshot(session):
        records = mcp_tool_json(await session.call_tool("eidolon_memory_list", {"limit": 100}))
        graph = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_kg_snapshot", {"max_triples": 100, "current_only": True}
            )
        )
        return records["records"], graph["triples"]

    async with mcp_session(first.mcp_url) as session:

        async def landed(s):
            records, triples = await snapshot(s)
            return any(r["value"] == fact for r in records) and any(
                t["subject"] == "person:restart" and t["object"] == "topic:oolong" for t in triples
            )

        assert await wait_for_visible(session, predicate=landed, timeout_s=30)
        before, triples_before = await snapshot(session)
        status = mcp_tool_json(
            await session.call_tool("eidolon_memory_command_status", {"request_id": request})
        )
        assert status["status"] == "applied"

    first.kill()
    assert first.process.returncode == 0, "restart must follow a completed graceful shutdown"
    pending_fact = "重启期间发布的记忆仍须处理"
    pending = await nats_publish_assertion(
        first.nats_url,
        user_id=first.user_id,
        text=pending_fact,
    )
    second = live_agent_runner(user_id=first.user_id, steward_mode="noop", keep_palace=True)
    assert second.palace_dir == first.palace_dir
    async with mcp_session(second.mcp_url) as session:
        assert await landed(session)
        after, triples_after = await snapshot(session)
        old_ids = {r["key"] for r in before if r["value"] == fact}
        assert {r["key"] for r in after if r["value"] == fact} == old_ids
        assert {(t["subject"], t["predicate"], t["object"]) for t in triples_before} <= {
            (t["subject"], t["predicate"], t["object"]) for t in triples_after
        }
        status = mcp_tool_json(
            await session.call_tool("eidolon_memory_command_status", {"request_id": request})
        )
        assert status["status"] == "applied"

        async def pending_applied(s):
            result = mcp_tool_json(
                await s.call_tool("eidolon_memory_command_status", {"request_id": pending})
            )
            return result.get("status") == "applied"

        assert await wait_for_visible(session, predicate=pending_applied, timeout_s=30)
        rows, _ = await snapshot(session)
        assert any(r["value"] == pending_fact for r in rows)

    async with mcp_session(second.agent_mcp_url) as session:
        recalled = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_recall_context",
                {
                    "query": fact,
                    "context": e2e_actor_context(second.user_id),
                    "voice": False,
                },
            )
        )
        assert not recalled.get("degraded"), recalled
        assert fact in recalled["context"]
