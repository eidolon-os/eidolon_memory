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
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

# In the monorepo workspace, exercise the sibling Agent checkout directly.
# Standalone eidolon-memory CI still skips cleanly through importorskip below.
_WORKSPACE_ROOT = Path(
    os.environ.get("EIDOLON_WORKSPACE_ROOT", str(Path(__file__).resolve().parents[4]))
).resolve()
_SIBLING_ROOTS = (
    _WORKSPACE_ROOT / "eidolon_sdk",
    _WORKSPACE_ROOT / "eidolon_agent",
)
_added_paths = [str(path) for path in _SIBLING_ROOTS if path.is_dir() and str(path) not in sys.path]
sys.path.extend(_added_paths)
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
    agent_context_compiler = pytest.importorskip(
        "eidolon_agent.domain.context.compiler",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_history_manager = pytest.importorskip(
        "eidolon_agent.domain.history.manager",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_turn_context = pytest.importorskip(
        "eidolon_agent.core.types.turn_context",
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
    agent_persona_realizer = pytest.importorskip(
        "eidolon_agent.domain.personas.realizer",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
    agent_turn_types = pytest.importorskip(
        "eidolon_agent.core.types.turn",
        reason="eidolon_agent package is required for the cross-repo agent memory e2e",
    )
finally:
    # Do not let the sibling checkout's regular ``tests`` package shadow this
    # repository's namespace package during full-suite pytest collection.
    for added_path in _added_paths:
        sys.path.remove(added_path)

MemoryEndpoint = agent_settings.MemoryEndpoint
NatsSettings = agent_settings.NatsSettings
MemoryQueryPlan = agent_memory_types.MemoryQueryPlan
ContextCompiler = agent_context_compiler.ContextCompiler
HistoryManager = agent_history_manager.HistoryManager
TurnContext = agent_turn_context.TurnContext
NatsEventBus = agent_nats_bus.NatsEventBus
MemoryRoutingTable = agent_discovery.MemoryRoutingTable
McpClientPool = agent_mcp_client.McpClientPool
MemoryNatsPublisher = agent_nats_pub.MemoryNatsPublisher
EidolonMemoryPort = agent_port_adapter.EidolonMemoryPort
PersonaRealizer = agent_persona_realizer.PersonaRealizer
TurnInput = agent_turn_types.TurnInput
TurnTrigger = agent_turn_types.TurnTrigger

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


class _E2EPersonas:
    """Keep persona deterministic while exercising real Commitment reads."""

    def __init__(self) -> None:
        self._realizer = PersonaRealizer()

    async def realize_context(self, **_kwargs):
        return SimpleNamespace(system_prompt="[PERSONA]\ne2e companion", debug_trace=())

    def realize_commitment_context(self, commitments):
        return self._realizer.realize_commitment_context(commitments)


def _turn_input(
    *,
    owner_id: str,
    companion_id: str,
    memory_realm_id: str,
    turn_id: str,
    text: str,
) -> TurnInput:
    return TurnInput(
        turn_id=turn_id,
        conversation_id="conversation-e2e-commitment",
        session_id="session-e2e",
        context=TurnContext(
            owner_id=owner_id,
            companion_id=companion_id,
            device_id="device-e2e",
            memory_realm_id=memory_realm_id,
            genome_id="genome-e2e",
            trace_id=f"trace-{turn_id}",
            request_id=f"request-{turn_id}",
        ),
        input_modality="text",
        trigger=TurnTrigger.USER_UTTERANCE,
        text=text,
    )


def _active_commitment_section(system_prompt: str) -> str:
    marker = "[ACTIVE COMMITMENTS]"
    if marker not in system_prompt:
        return ""
    tail = system_prompt.split(marker, 1)[1]
    return tail.split("\n\n[", 1)[0]


def _latency_percentiles(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    assert ordered

    def _pct(fraction: float) -> float:
        index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
        return round(ordered[index], 3)

    return {
        "count": len(ordered),
        "p50": _pct(0.50),
        "p95": _pct(0.95),
        "p99": _pct(0.99),
        "max": round(ordered[-1], 3),
    }


async def test_agent_memory_port_writes_and_recalls_default_user(live_agent_runner) -> None:
    handle = live_agent_runner(
        user_id=MEMORY_SPACE_ID,

        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=MEMORY_SPACE_ID,
                mcp_url=handle.agent_mcp_url,
                ops_mcp_url=handle.mcp_url,
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


async def test_agent_personal_recall_latency_distribution(live_agent_runner) -> None:
    """Measure the public Agent read path with the natural personal-question budget."""
    handle = live_agent_runner(
        user_id="e2e_agent_personal_recall_perf",
        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=handle.user_id,
                mcp_url=handle.agent_mcp_url,
                ops_mcp_url=handle.mcp_url,
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
    marker = f"火龙果-{uuid.uuid4().hex[:8]}"
    owner_id = "owner-perf"
    companion_id = "companion-perf"
    query = f"我最喜欢的水果是不是 {marker}？"
    try:
        write_started = time.perf_counter()
        await port.assert_fact(
            owner_id,
            companion_id,
            handle.user_id,
            "owner",
            "likes",
            marker,
            source_event_id=f"turn-{marker}",
            tool_call_id=f"call-{marker}",
            confidence=0.99,
        )

        latest_recall = None

        async def _fact_visible() -> bool:
            nonlocal latest_recall
            latest_recall = await port.recall_context(
                owner_id,
                query,
                memory_realm_id=handle.user_id,
                plan=MemoryQueryPlan(semantic_k=5, voice=True),
                timeout_s=4.0,
                companion_id=companion_id,
                device_id="device-perf",
                session_id="session-perf",
            )
            return not latest_recall.degraded and marker in latest_recall.context

        assert await _wait_for_true(_fact_visible, timeout_s=30, poll_interval_s=0.05)
        write_visibility_ms = (time.perf_counter() - write_started) * 1000

        compiler = ContextCompiler(
            personas_service=_E2EPersonas(),
            instance_locator=lambda _owner, companion, _conversation: (
                companion,
                "genome-e2e",
            ),
            history_manager=HistoryManager(),
            memory_port=port,
            memory_timeout_s=0.5,
            explicit_memory_timeout_s=4.0,
            active_commitment_limit=1,
            active_commitment_timeout_s=0.5,
            context_budget_mode="disabled",
        )

        samples: list[dict[str, float]] = []
        for index in range(60):
            turn = _turn_input(
                owner_id=owner_id,
                companion_id=companion_id,
                memory_realm_id=handle.user_id,
                turn_id=f"turn-perf-{index}-{marker}",
                text=query,
            )
            started = time.perf_counter()
            messages = await compiler.compile(turn)
            compiler_total_ms = (time.perf_counter() - started) * 1000
            trace = turn.metadata["memory_trace"]
            assert trace["timeout_ms"] == 4000
            assert trace["degraded"] is False, trace
            assert trace["context_injected"] is True
            assert marker in messages[0].content
            sample = {
                "compiler_total_ms": compiler_total_ms,
                "agent_memory_elapsed_ms": float(trace["elapsed_ms"]),
            }
            for name, value in (trace.get("backend_trace") or {}).items():
                sample[f"backend_{name}"] = float(value)
            samples.append(sample)

        metrics = {
            name: _latency_percentiles(
                [sample[name] for sample in samples if name in sample]
            )
            for name in sorted({key for sample in samples for key in sample})
        }
        report = {
            "write_visibility_ms": round(write_visibility_ms, 3),
            "query": query,
            "timeout_classification_ms": 4000,
            "metrics": metrics,
        }
        print("AGENT_MEMORY_PERF=" + json.dumps(report, ensure_ascii=False, sort_keys=True))

        assert metrics["agent_memory_elapsed_ms"]["p95"] < 500
        assert metrics["compiler_total_ms"]["p95"] < 500
    finally:
        await port.close()
        await bus.close()


async def test_agent_commitment_product_read_is_active_only(live_agent_runner) -> None:
    """Real NATS + Realm MCP: active is injected; fulfilled disappears."""
    handle = live_agent_runner(
        user_id="e2e_agent_commitment_context",

        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=handle.user_id,
                mcp_url=handle.agent_mcp_url,
                ops_mcp_url=handle.mcp_url,
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
    later_marker = f"agent-commitment-later-{uuid.uuid4().hex[:8]}"
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
        later_request_id = await port.apply_commitment(
            owner_id,
            companion_id,
            handle.user_id,
            "小忆",
            "promised",
            f"下个月陪 owner 去博物馆 {later_marker}",
            f"我答应下个月陪你去博物馆 {later_marker}",
            source_event_id=f"turn-create-{later_marker}",
            tool_call_id=f"call-create-{later_marker}",
            operation="confirm",
            beneficiaries=[owner_id],
            due_at="2026-08-18T09:00:00+08:00",
            status="confirmed",
        )
        assert later_request_id

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
            return current is not None and any(
                later_marker in item.action for item in result.commitments
            )

        assert await _wait_for_true(_active_visible, timeout_s=30)
        assert current is not None
        assert current.status == "confirmed"
        assert set(current.participants) == {"朋友甲", "朋友乙"}

        prioritized = await port.read_active_commitments(
            owner_id,
            companion_id=companion_id,
            memory_realm_id=handle.user_id,
            limit=1,
            timeout_s=5.0,
        )
        assert prioritized.degraded is False
        assert len(prioritized.commitments) == 1
        assert prioritized.total == 2
        assert prioritized.truncated is True
        assert marker in prioritized.commitments[0].action

        # Exercise the actual product path. ContextCompiler performs recall and
        # Commitment reads concurrently through the same Realm-bound MCP pool,
        # then PersonaRealizer renders only the bounded active set.
        compiler = ContextCompiler(
            personas_service=_E2EPersonas(),
            instance_locator=lambda _owner, companion, _conversation: (
                companion,
                "genome-e2e",
            ),
            history_manager=HistoryManager(),
            memory_port=port,
            memory_timeout_s=5.0,
            active_commitment_limit=1,
            active_commitment_timeout_s=5.0,
            context_budget_mode="disabled",
        )
        before_turn = _turn_input(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=handle.user_id,
            turn_id=f"turn-context-before-{marker}",
            text="今天聊点别的",
        )
        before_messages = await compiler.compile(before_turn)
        before_active = _active_commitment_section(before_messages[0].content)
        assert marker in before_active
        assert later_marker not in before_active
        assert "actionability=must_not_execute" in before_active
        assert before_turn.metadata["commitment_context_trace"]["total"] == 2
        assert before_turn.metadata["commitment_context_trace"]["truncated"] is True
        assert before_turn.metadata["commitment_context_trace"]["context_injected"] is True

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

        after_fulfilment = await port.read_active_commitments(
            owner_id,
            companion_id=companion_id,
            memory_realm_id=handle.user_id,
            limit=1,
            timeout_s=5.0,
        )
        assert after_fulfilment.degraded is False
        assert len(after_fulfilment.commitments) == 1
        assert after_fulfilment.total == 1
        assert after_fulfilment.truncated is False
        assert later_marker in after_fulfilment.commitments[0].action

        after_turn = _turn_input(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=handle.user_id,
            turn_id=f"turn-context-after-{marker}",
            text="继续聊点别的",
        )
        after_messages = await compiler.compile(after_turn)
        after_active = _active_commitment_section(after_messages[0].content)
        assert marker not in after_active
        assert later_marker in after_active
        assert after_turn.metadata["commitment_context_trace"]["total"] == 1
        assert after_turn.metadata["commitment_context_trace"]["truncated"] is False
        assert after_turn.metadata["commitment_context_trace"]["context_injected"] is True
    finally:
        await port.close()
        await bus.close()


async def test_agent_memory_port_delete_is_previewed_and_terminally_applied(
    live_agent_runner,
    mcp_session,
) -> None:
    handle = live_agent_runner(
        user_id="e2e_agent_privacy_terminal",

        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=handle.user_id,
                mcp_url=handle.agent_mcp_url,
                ops_mcp_url=handle.mcp_url,
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

        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=handle.user_id,
                mcp_url=handle.agent_mcp_url,
                ops_mcp_url=handle.mcp_url,
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

            invalidation_request_id = await port.invalidate_fact(
                "e2e",
                "e2e",
                handle.user_id,
                "self",
                "likes",
                marker,
                source_event_id=f"turn-invalidate-{marker}",
                tool_call_id=f"call-invalidate-{marker}",
            )
            invalidated = None

            async def _invalidation_applied() -> bool:
                nonlocal invalidated
                invalidated = _mcp_tool_json(
                    await session.call_tool(
                        "eidolon_memory_command_status",
                        {"request_id": invalidation_request_id},
                    )
                )
                return (
                    isinstance(invalidated, dict)
                    and invalidated.get("status") == "applied"
                )

            assert await _wait_for_true(_invalidation_applied, timeout_s=30), invalidated
            assert str(invalidated.get("resource_id", "")).startswith(
                "invalidated:fact:"
            )

            repair_request_id = await port.reactivate_fact(
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
                "reactivated:fact:"
            )

            repaired_list = _mcp_tool_json(
                await session.call_tool(
                    "eidolon_memory_list",
                    {"limit": 100, "include_private": False},
                )
            )
            repaired_records = (repaired_list or {}).get("records") or []
            repaired_values = [
                str(record.get("value") or "")
                for record in repaired_records
                if (record.get("metadata") or {}).get("privacy") != "do_not_recall"
            ]
            assert repaired_values.count(f"self likes {marker}") == 1
            archived_values = [
                str(record.get("value") or "")
                for record in repaired_records
                if (record.get("metadata") or {}).get("privacy") == "do_not_recall"
            ]
            assert archived_values.count(f"self likes {marker}") == 1

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
