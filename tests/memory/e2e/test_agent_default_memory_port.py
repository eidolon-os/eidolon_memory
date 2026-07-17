"""Agent-side e2e for the default owner's memory read/write path.

This test intentionally uses the agent's public memory port adapter while the
memory service runs as a real subprocess:

    agent EidolonMemoryPort.write_turn -> NATS -> eidolon-memory-agent
      -> MCP recall via agent McpClientPool -> rendered working-memory context

It avoids LLM / long-term extraction assumptions by asserting against the
process-local working-memory section.
"""

import asyncio
import inspect
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

# In the monorepo workspace, exercise the sibling Agent checkout directly.
# Standalone eidolon-memory CI still skips cleanly through importorskip below.
_AGENT_ROOT = Path(__file__).resolve().parents[4] / "eidolon_agent"
_agent_path_added = _AGENT_ROOT.is_dir() and str(_AGENT_ROOT) not in sys.path
if _agent_path_added:
    sys.path.append(str(_AGENT_ROOT))
try:
    agent_settings = pytest.importorskip(
        "eidolon_agent.config.settings",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_memory_types = pytest.importorskip(
        "eidolon_agent.core.types.memory",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_nats_bus = pytest.importorskip(
        "eidolon_agent.infra.events.nats_bus",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_discovery = pytest.importorskip(
        "eidolon_agent.infra.memory.discovery",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_mcp_client = pytest.importorskip(
        "eidolon_agent.infra.memory.mcp_client",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_nats_pub = pytest.importorskip(
        "eidolon_agent.infra.memory.nats_pub",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_port_adapter = pytest.importorskip(
        "eidolon_agent.infra.memory.port_adapter",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
finally:
    # Do not let the sibling checkout's regular ``tests`` package shadow this
    # repository's namespace package during full-suite pytest collection.
    if _agent_path_added:
        sys.path.remove(str(_AGENT_ROOT))

MemoryEndpoint = agent_settings.MemoryEndpoint
NatsSettings = agent_settings.NatsSettings
MemoryQueryPlan = agent_memory_types.MemoryQueryPlan
NatsEventBus = agent_nats_bus.NatsEventBus
MemoryRoutingTable = agent_discovery.MemoryRoutingTable
McpClientPool = agent_mcp_client.McpClientPool
MemoryNatsPublisher = agent_nats_pub.MemoryNatsPublisher
EidolonMemoryPort = agent_port_adapter.EidolonMemoryPort

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

MEMORY_SPACE_ID = "default.default.agent_e2e"
OWNER_USER_ID = "default"
COMPANION_ID = "agent_e2e"
SESSION_ID = "agent-e2e-default"


async def _wait_for_true(
    predicate,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.5,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(poll_interval_s)
    return False


def _mcp_tool_json(result):
    if not getattr(result, "content", None):
        return None
    text = getattr(result.content[0], "text", "") or ""
    if not text:
        return None
    payload = json.loads(text)
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


async def test_agent_memory_port_writes_and_recalls_default_user(live_agent_runner) -> None:
    handle = live_agent_runner(
        user_id=MEMORY_SPACE_ID,
        port=19130,
        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=MEMORY_SPACE_ID,
                mcp_url=handle.mcp_url,
            )
        ],
        nats=NatsSettings(url=handle.nats_url),
    )
    bus = NatsEventBus(handle.nats_url)
    pool = McpClientPool(routes=routes)
    port = EidolonMemoryPort(
        pool=pool,
        publisher=MemoryNatsPublisher(event_bus=bus, routes=routes),
    )
    marker = f"agent-default-memory-e2e-{uuid.uuid4().hex[:8]}"
    try:
        await port.write_turn(
            OWNER_USER_ID,
            COMPANION_ID,
            MEMORY_SPACE_ID,
            "default-device",
            SESSION_ID,
            f"turn-{marker}",
            f"这只是端到端测试标记: {marker}, 不要长期记住。",
            "好的, 我只在这次会话里保留它。",
            metadata={"source": "agent-default-memory-e2e"},
        )

        async def _recall_contains_marker() -> bool:
            result = await port.recall_context(
                OWNER_USER_ID,
                "刚才的端到端测试标记是什么?",
                memory_realm_id=MEMORY_SPACE_ID,
                plan=MemoryQueryPlan(semantic_k=5, voice=False),
                timeout_s=5.0,
                companion_id=COMPANION_ID,
                device_id="default-device",
                session_id=SESSION_ID,
            )
            assert result.degraded is False
            return marker in result.context

        assert await _wait_for_true(
            _recall_contains_marker,
            timeout_s=30,
            poll_interval_s=0.5,
        ), f"agent memory port did not recall the turn written for {MEMORY_SPACE_ID}"
    finally:
        await port.close()
        await bus.close()


async def test_agent_commitment_product_read_is_active_only(live_agent_runner) -> None:
    """Real NATS + Realm MCP: active is injected; fulfilled disappears."""
    handle = live_agent_runner(
        user_id="e2e_agent_commitment_context",
        port=19133,
        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=handle.user_id,
                mcp_url=handle.mcp_url,
            )
        ],
        nats=NatsSettings(url=handle.nats_url),
    )
    bus = NatsEventBus(handle.nats_url)
    pool = McpClientPool(routes=routes)
    port = EidolonMemoryPort(
        pool=pool,
        publisher=MemoryNatsPublisher(event_bus=bus, routes=routes),
    )
    marker = f"agent-commitment-context-{uuid.uuid4().hex[:8]}"
    owner_id = "owner-e2e"
    companion_id = "companion-e2e"
    try:
        request_id = await port.apply_commitment(
            owner_id,
            companion_id,
            handle.user_id,
            "小忆",
            "promised",
            f"周六陪 owner 去恐龙园 {marker}",
            f"我答应周六陪你去恐龙园 {marker}",
            source_event_id=f"turn-create-{marker}",
            tool_call_id=f"call-create-{marker}",
            operation="confirm",
            beneficiaries=[owner_id],
            participants=["朋友甲", "朋友乙"],
            due_at="2026-07-18T09:00:00+08:00",
            status="confirmed",
        )
        assert request_id

        current = None

        async def _active_visible() -> bool:
            nonlocal current
            result = await port.read_active_commitments(
                owner_id,
                companion_id=companion_id,
                memory_realm_id=handle.user_id,
                device_id="device-e2e",
                session_id="session-e2e",
                limit=3,
                timeout_s=5.0,
            )
            assert result.degraded is False, result.degraded_reason
            current = next(
                (
                    item
                    for item in result.commitments
                    if marker in item.action
                ),
                None,
            )
            return current is not None

        assert await _wait_for_true(_active_visible, timeout_s=30)
        assert current is not None
        assert current.status == "confirmed"
        assert set(current.participants) == {"朋友甲", "朋友乙"}

        # ContextCompiler performs these reads concurrently on the same
        # Realm-bound MCP session. Exercise that transport shape against the
        # real process instead of assuming ClientSession multiplexing works.
        recall_result, commitment_result = await asyncio.gather(
            port.recall_context(
                owner_id,
                "今天聊点别的",
                memory_realm_id=handle.user_id,
                plan=MemoryQueryPlan(semantic_k=3, voice=False),
                companion_id=companion_id,
                device_id="device-e2e",
                session_id="session-e2e",
                timeout_s=5.0,
            ),
            port.read_active_commitments(
                owner_id,
                companion_id=companion_id,
                memory_realm_id=handle.user_id,
                device_id="device-e2e",
                session_id="session-e2e",
                limit=3,
                timeout_s=5.0,
            ),
        )
        assert recall_result.degraded is False
        assert commitment_result.degraded is False
        assert any(marker in item.action for item in commitment_result.commitments)

        fulfil_request_id = await port.apply_commitment(
            owner_id,
            companion_id,
            handle.user_id,
            "小忆",
            "promised",
            current.action,
            f"我们已经去过恐龙园了 {marker}",
            source_event_id=f"turn-fulfil-{marker}",
            tool_call_id=f"call-fulfil-{marker}",
            operation="update",
            target_id=current.commitment_id,
            status="fulfilled",
        )
        assert fulfil_request_id

        async def _terminal_absent() -> bool:
            result = await port.read_active_commitments(
                owner_id,
                companion_id=companion_id,
                memory_realm_id=handle.user_id,
                limit=3,
                timeout_s=5.0,
            )
            assert result.degraded is False, result.degraded_reason
            return all(marker not in item.action for item in result.commitments)

        assert await _wait_for_true(_terminal_absent, timeout_s=30)
    finally:
        await port.close()
        await bus.close()


async def test_agent_memory_port_delete_is_previewed_and_terminally_applied(
    live_agent_runner,
    mcp_session,
) -> None:
    handle = live_agent_runner(
        user_id="e2e_agent_privacy_terminal",
        port=19131,
        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=handle.user_id,
                mcp_url=handle.mcp_url,
            )
        ],
        nats=NatsSettings(url=handle.nats_url),
    )
    bus = NatsEventBus(handle.nats_url)
    pool = McpClientPool(routes=routes)
    port = EidolonMemoryPort(
        pool=pool,
        publisher=MemoryNatsPublisher(event_bus=bus, routes=routes),
    )
    marker = f"agent-privacy-{uuid.uuid4().hex[:8]}"
    facts = [f"{marker} 工作记录", f"{marker} 旅行记录"]
    resources_closed = False
    try:
        for index, fact in enumerate(facts):
            await port.write_confirmed_fact(
                "e2e",
                "e2e",
                handle.user_id,
                "e2e",
                "e2e",
                text=fact,
                source_event_id=f"turn-{marker}",
                tool_call_id=f"call-{index}",
            )

        latest_preview = None

        async def _preview_has_both() -> bool:
            nonlocal latest_preview
            latest_preview = await port.preview_forget(
                "e2e",
                "e2e",
                handle.user_id,
                "e2e",
                marker,
                action="delete",
                session_id="e2e",
            )
            return (
                latest_preview.status == "preview"
                and len(latest_preview.candidates) == 2
            )

        assert await _wait_for_true(_preview_has_both, timeout_s=30)
        assert latest_preview is not None
        assert latest_preview.requires_explicit_confirmation is True

        # A second preview still resolves both rows: preview is read-only.
        second_preview = await port.preview_forget(
            "e2e",
            "e2e",
            handle.user_id,
            "e2e",
            marker,
            action="delete",
            session_id="e2e",
        )
        assert len(second_preview.candidates) == 2

        outcome = await port.confirm_forget(
            "e2e",
            "e2e",
            handle.user_id,
            "e2e",
            second_preview.confirmation_token,
            session_id="e2e",
            wait_applied_seconds=5.0,
        )
        assert outcome.status == "applied"
        assert outcome.request_id
        assert len(outcome.drawer_ids) == 2

        # Close the Agent-owned MCP session before opening an independent
        # verification client; this matches separate production callers and
        # keeps AnyIO cancel scopes properly nested.
        await port.close()
        await bus.close()
        resources_closed = True

        async with mcp_session(handle.mcp_url) as session:
            payload = _mcp_tool_json(
                await session.call_tool(
                    "eidolon_memory_list",
                    {"limit": 100, "include_private": False},
                )
            )
            values = {
                str(record.get("value") or "")
                for record in (payload or {}).get("records") or []
            }
            assert all(fact not in values for fact in facts)
    finally:
        if not resources_closed:
            await port.close()
            await bus.close()


async def test_agent_structured_intent_projects_drawer_and_kg_with_terminal_status(
    live_agent_runner,
    mcp_session,
) -> None:
    handle = live_agent_runner(
        user_id="e2e_agent_structured_intent",
        port=19132,
        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=handle.user_id,
                mcp_url=handle.mcp_url,
            )
        ],
        nats=NatsSettings(url=handle.nats_url),
    )
    bus = NatsEventBus(handle.nats_url)
    pool = McpClientPool(routes=routes)
    port = EidolonMemoryPort(
        pool=pool,
        publisher=MemoryNatsPublisher(event_bus=bus, routes=routes),
    )
    marker = f"oolong-{uuid.uuid4().hex[:8]}"
    turn_id = f"turn-{marker}"
    call_id = f"call-{marker}"
    try:
        request_id = await port.assert_fact(
            "e2e",
            "e2e",
            handle.user_id,
            "self",
            "likes",
            marker,
            source_event_id=turn_id,
            tool_call_id=call_id,
            confidence=0.99,
        )
        assert request_id

        async with mcp_session(handle.mcp_url) as session:
            latest_status = None

            async def _applied() -> bool:
                nonlocal latest_status
                latest_status = _mcp_tool_json(
                    await session.call_tool(
                        "eidolon_memory_command_status",
                        {"request_id": request_id},
                    )
                )
                return (
                    isinstance(latest_status, dict)
                    and latest_status.get("status") == "applied"
                )

            assert await _wait_for_true(_applied, timeout_s=30)
            assert latest_status is not None
            assert str(latest_status.get("resource_id", "")).startswith(
                "memoryintent:fact:"
            )

            second_request_id = await port.assert_fact(
                "e2e",
                "e2e",
                handle.user_id,
                "self",
                "likes",
                marker,
                source_event_id=f"turn-confirm-again-{marker}",
                tool_call_id=f"call-confirm-again-{marker}",
                confidence=0.99,
            )
            second_status = None

            async def _second_applied() -> bool:
                nonlocal second_status
                second_status = _mcp_tool_json(
                    await session.call_tool(
                        "eidolon_memory_command_status",
                        {"request_id": second_request_id},
                    )
                )
                return (
                    isinstance(second_status, dict)
                    and second_status.get("status") == "applied"
                )

            assert await _wait_for_true(_second_applied, timeout_s=30)
            assert str(second_status.get("resource_id", "")).endswith(
                ":evidence:2"
            )

            listed = _mcp_tool_json(
                await session.call_tool(
                    "eidolon_memory_list",
                    {"limit": 100, "include_private": True},
                )
            )
            values = [
                str(record.get("value") or "")
                for record in (listed or {}).get("records") or []
            ]
            assert values.count(f"self likes {marker}") == 1

            kg_result = _mcp_tool_json(
                await session.call_tool(
                    "eidolon_memory_kg_query_entity",
                    {"name": "self"},
                )
            )
            matching_triples = [
                triple
                for triple in (kg_result or {}).get("triples") or []
                if triple.get("predicate") == "likes"
                and triple.get("object") == marker
            ]
            assert len(matching_triples) == 1

            invalidated = _mcp_tool_json(
                await session.call_tool(
                    "eidolon_memory_kg_invalidate",
                    {
                        "subject": "self",
                        "predicate": "likes",
                        "object": marker,
                        "wait_visible_seconds": 5.0,
                    },
                )
            )
            assert invalidated.get("status") == "applied"

            repair_request_id = await port.assert_fact(
                "e2e",
                "e2e",
                handle.user_id,
                "self",
                "likes",
                marker,
                source_event_id=f"turn-repair-{marker}",
                tool_call_id=f"call-repair-{marker}",
                confidence=0.99,
            )
            repair_status = None

            async def _repair_applied() -> bool:
                nonlocal repair_status
                repair_status = _mcp_tool_json(
                    await session.call_tool(
                        "eidolon_memory_command_status",
                        {"request_id": repair_request_id},
                    )
                )
                return (
                    isinstance(repair_status, dict)
                    and repair_status.get("status") == "applied"
                )

            assert await _wait_for_true(_repair_applied, timeout_s=30)
            assert str(repair_status.get("resource_id", "")).startswith(
                "memoryintent:fact:"
            )

            repaired_list = _mcp_tool_json(
                await session.call_tool(
                    "eidolon_memory_list",
                    {"limit": 100, "include_private": True},
                )
            )
            repaired_values = [
                str(record.get("value") or "")
                for record in (repaired_list or {}).get("records") or []
            ]
            assert repaired_values.count(f"self likes {marker}") == 1

            repaired_kg = _mcp_tool_json(
                await session.call_tool(
                    "eidolon_memory_kg_query_entity",
                    {"name": "self"},
                )
            )
            repaired_triples = [
                triple
                for triple in (repaired_kg or {}).get("triples") or []
                if triple.get("predicate") == "likes"
                and triple.get("object") == marker
            ]
            assert len(repaired_triples) == 1
    finally:
        await port.close()
        await bus.close()
