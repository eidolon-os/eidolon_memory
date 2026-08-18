"""Real NATS → DLQ ledger → replay/resolve operational closure."""

from __future__ import annotations

import pytest

from tests.memory.e2e.conftest import (
    mcp_tool_json,
    nats_publish_kg_invalidate,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_terminal_command_failure_can_be_inspected_replayed_and_resolved(
    live_agent_runner,
    mcp_session,
) -> None:
    handle = live_agent_runner(
        user_id="e2e_dlq_recovery",
        
        steward_mode="noop",
        extra_settings={"nats": {"worker_max_deliveries": 1}},
    )
    request_id = await nats_publish_kg_invalidate(
        handle.nats_url,
        user_id=handle.user_id,
        subject="self",
        predicate="likes",
        obj="never-existed",
    )

    async with mcp_session(handle.mcp_url) as session:
        async def _failed(s) -> bool:
            result = mcp_tool_json(
                await s.call_tool(
                    "eidolon_memory_command_status", {"request_id": request_id}
                )
            )
            return isinstance(result, dict) and result.get("status") == "failed"

        assert await wait_for_visible(session, predicate=_failed, timeout_s=30)

        async def _unresolved(s) -> bool:
            result = mcp_tool_json(
                await s.call_tool("eidolon_memory_dlq_list", {"state": "unresolved"})
            )
            return isinstance(result, dict) and len(result.get("records") or []) >= 1

        assert await wait_for_visible(session, predicate=_unresolved, timeout_s=30)
        listed = mcp_tool_json(
            await session.call_tool("eidolon_memory_dlq_list", {"state": "unresolved"})
        )
        first = listed["records"][0]
        detail = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_dlq_detail", {"entry_id": first["entry_id"]}
            )
        )
        assert "payload" not in detail["record"]
        assert detail["record"]["payload_size"] > 0

        replayed = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_dlq_replay", {"entry_id": first["entry_id"]}
            )
        )
        assert replayed["status"] == "replayed"
        duplicate = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_dlq_replay", {"entry_id": first["entry_id"]}
            )
        )
        assert duplicate["status"] == "not_replayable"

        # The replayed invalid command fails again and creates a fresh
        # unresolved entry; an operator can explicitly resolve that one.
        async def _fresh_dead_letter(s) -> bool:
            result = mcp_tool_json(
                await s.call_tool("eidolon_memory_dlq_list", {"state": "unresolved"})
            )
            records = result.get("records") or [] if isinstance(result, dict) else []
            return any(record["entry_id"] != first["entry_id"] for record in records)

        assert await wait_for_visible(
            session, predicate=_fresh_dead_letter, timeout_s=30
        )
        current = mcp_tool_json(
            await session.call_tool("eidolon_memory_dlq_list", {"state": "unresolved"})
        )
        second = next(
            record for record in current["records"]
            if record["entry_id"] != first["entry_id"]
        )
        resolved = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_dlq_resolve",
                {"entry_id": second["entry_id"], "note": "invalid test command"},
            )
        )
        assert resolved["status"] == "resolved"
