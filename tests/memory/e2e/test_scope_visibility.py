"""Real NATS writes/MCP recalls enforce companion, device, and realm boundaries."""

import pytest

from tests.memory.e2e.conftest import (
    e2e_actor_context,
    mcp_tool_json,
    nats_publish_assertion,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_real_recall_respects_companion_device_and_realm(live_agent_runner, mcp_session):
    handle = live_agent_runner(user_id="scope-main")
    other = live_agent_runner(user_id="scope-other")
    facts = ("visible-to-all-devices", "visible-on-phone", "other-companion-secret")
    for fact, companion, attrs in (
        (facts[0], "e2e", {}),
        (
            facts[1],
            "e2e",
            {"scope": "device", "visibility": "current_device", "source_device_id": "phone"},
        ),
        (facts[2], "other", {}),
    ):
        await nats_publish_assertion(
            handle.nats_url,
            user_id=handle.user_id,
            text=fact,
            companion_id=companion,
            attributes=attrs,
        )
    await nats_publish_assertion(other.nats_url, user_id=other.user_id, text="other-realm-secret")
    async with mcp_session(handle.mcp_url) as ops:

        async def landed(s):
            data = mcp_tool_json(await s.call_tool("eidolon_memory_list", {"limit": 100}))
            return {r["value"] for r in data["records"]} == set(facts)

        assert await wait_for_visible(ops, predicate=landed, timeout_s=30)
    async with mcp_session(handle.agent_mcp_url) as reader:
        for device, companion, expected in (
            ("phone", "e2e", {facts[0], facts[1]}),
            ("laptop", "e2e", {facts[0]}),
            ("", "e2e", {facts[0]}),
            ("phone", "other", {facts[2]}),
        ):
            result = mcp_tool_json(
                await reader.call_tool(
                    "eidolon_memory_recall_context",
                    {
                        "query": "visible",
                        "voice": False,
                        "top_k": 20,
                        "context": e2e_actor_context(
                            handle.user_id, device_id=device, companion_id=companion
                        ),
                    },
                )
            )
            assert not result.get("degraded"), result
            assert {r["value"] for r in result["records"]} == expected
            assert "other-realm-secret" not in str(result)
