"""Personal bindings over real MCP, NATS and independent Realm processes.

Administrative commands seed deterministic facts; this does not simulate Data
authorization or conversational LLM extraction. Those are separate boundaries.
"""

import pytest
from eidolon_memory_contracts import MemoryActorContext

from tests.memory.e2e.conftest import (
    e2e_actor_context,
    mcp_tool_json,
    nats_publish_assertion,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_person_realms_keep_identical_facts_isolated_through_delete_and_restart(
    live_agent_runner, mcp_session
):
    a = live_agent_runner(user_id="personal-a")
    b = live_agent_runner(user_id="personal-b")
    common = "personal-isolation-common-tea"
    secrets = {"a": "personal-secret-purple-seagull", "b": "personal-secret-golden-fox"}

    def context(handle, person):
        return {
            **e2e_actor_context(handle.user_id, session_id=f"session-{person}"),
            "person_id": person,
            "identity_epoch": 1,
        }

    async def values(session):
        result = mcp_tool_json(await session.call_tool("eidolon_memory_list", {"limit": 100}))
        return {row["value"] for row in result["records"]}

    async def check_reader(handle, person, expected):
        ctx = context(handle, person)
        async with mcp_session(handle.agent_mcp_url) as reader:
            result = mcp_tool_json(
                await reader.call_tool(
                    "eidolon_memory_recall_context",
                    {"query": "personal", "voice": False, "top_k": 20, "context": ctx},
                )
            )
            assert not result.get("degraded"), result
            assert result["identity_binding"] == MemoryActorContext(**ctx).identity_binding()
            assert {row["value"] for row in result["records"]} == expected
            commitments = mcp_tool_json(
                await reader.call_tool("eidolon_memory_active_commitments", {"context": ctx})
            )
            assert commitments["identity_binding"] == result["identity_binding"]
            # A wrong endpoint must reject the Realm, not merely echo its binding.
            wrong = context(b if person == "a" else a, person)
            refused = await reader.call_tool(
                "eidolon_memory_recall_context", {"query": "personal", "context": wrong}
            )
            assert refused.isError, refused

    for handle, person in ((a, "a"), (b, "b")):
        for fact in (common, secrets[person]):
            await nats_publish_assertion(handle.nats_url, user_id=handle.user_id, text=fact)
        async with mcp_session(handle.mcp_url) as ops:

            async def landed(session):
                return await values(session) == {common, secrets[person]}

            assert await wait_for_visible(ops, predicate=landed, timeout_s=30)
        await check_reader(handle, person, {common, secrets[person]})

    # Forget through the existing operator protocol, not direct storage mutation.
    async with mcp_session(a.mcp_url) as ops:
        preview = mcp_tool_json(
            await ops.call_tool(
                "eidolon_memory_forget_preview", {"target": common, "action": "delete"}
            )
        )
        assert preview["status"] == "preview", preview
        deleted = mcp_tool_json(
            await ops.call_tool(
                "eidolon_memory_forget_confirm",
                {"confirmation_token": preview["confirmation_token"], "wait_applied_seconds": 2.0},
            )
        )
        assert deleted["status"] in {"accepted", "applied"}, deleted

        async def gone(session):
            return await values(session) == {secrets["a"]}

        assert await wait_for_visible(ops, predicate=gone, timeout_s=30)

    await check_reader(a, "a", {secrets["a"]})
    await check_reader(b, "b", {common, secrets["b"]})
    a.kill()
    assert a.process.returncode == 0
    a = live_agent_runner(user_id=a.user_id, keep_palace=True)
    await check_reader(a, "a", {secrets["a"]})
    await check_reader(b, "b", {common, secrets["b"]})
