"""Agent-side E2E for durable Memory read/write and context compilation.

This test intentionally uses the agent's public memory port adapter while the
memory service runs as a real subprocess:

    Agent HistoryFanout -> NATS -> Memory extraction/projection
      -> Agent MCP recall -> ContextCompiler prompt

The deterministic test steward replaces only semantic extraction. Transport,
durable projection, voice recall and Agent prompt assembly are real.
"""

import asyncio
import inspect
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.memory.e2e.conftest import (
    nats_publish_assertion,
    nats_publish_commitment,
)

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
    agent_history_fanout = pytest.importorskip(
        "eidolon_agent.domain.history.fanout",
        reason="eidolon_agent package is required for the cross-repo Agent fanout e2e",
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
HistoryFanout = agent_history_fanout.HistoryFanout
EidolonMemoryPort = agent_port_adapter.EidolonMemoryPort
PersonaRealizer = agent_persona_realizer.PersonaRealizer
TurnInput = agent_turn_types.TurnInput
TurnTrigger = agent_turn_types.TurnTrigger

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

MEMORY_SPACE_ID = "default.default.agent_e2e"
OWNER_USER_ID = "default"
COMPANION_ID = "agent_e2e"


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
    session_id: str = "session-e2e",
    conversation_id: str = "conversation-e2e-commitment",
    input_modality: str = "text",
) -> TurnInput:
    return TurnInput(
        turn_id=turn_id,
        conversation_id=conversation_id,
        session_id=session_id,
        context=TurnContext(
            owner_id=owner_id,
            companion_id=companion_id,
            device_id="device-e2e",
            memory_realm_id=memory_realm_id,
            genome_id="genome-e2e",
            trace_id=f"trace-{turn_id}",
            request_id=f"request-{turn_id}",
        ),
        input_modality=input_modality,
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


async def test_natural_turn_is_recalled_by_voice_after_device_reenters(
    live_agent_runner,
) -> None:
    handle = live_agent_runner(
        user_id=MEMORY_SPACE_ID,
        steward_mode="test-verbatim",
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
    port = EidolonMemoryPort(pool=pool)
    fanout = HistoryFanout(event_bus=bus, memory_routes=routes)
    marker = f"agent-default-memory-e2e-{uuid.uuid4().hex[:8]}"
    session_a = f"dispatch-a-{marker}"
    session_b = f"dispatch-b-{marker}"
    try:
        write_status = await fanout.publish_turn(
            owner_id=OWNER_USER_ID,
            companion_id=COMPANION_ID,
            memory_realm_id=MEMORY_SPACE_ID,
            device_id="default-device",
            session_id=session_a,
            turn_id=f"turn-{marker}",
            user_text=f"书房那盆绿植叫{marker}。",
            assistant_text="听起来很有画面。",
            timestamp_iso=datetime.now(UTC).isoformat(),
            trace_id=f"trace-{marker}",
            metadata={"source": "agent-default-memory-e2e"},
        )
        assert write_status.state == "published", write_status

        async def _recall_contains_marker() -> bool:
            result = await port.recall_context(
                OWNER_USER_ID,
                marker,
                memory_realm_id=MEMORY_SPACE_ID,
                # Match ContextCompiler's product plan: ordinary personal
                # recall always queries the authenticated self node in
                # parallel with semantic retrieval.
                plan=MemoryQueryPlan(
                    semantic_k=5,
                    voice=True,
                    kg_subjects=("self",),
                ),
                timeout_s=5.0,
                companion_id=COMPANION_ID,
                device_id="default-device",
                session_id=session_b,
            )
            assert result.degraded is False
            return marker in result.context

        assert await _wait_for_true(
            _recall_contains_marker,
            timeout_s=30,
            poll_interval_s=0.5,
        ), "Agent did not recall session A's durable fact from voice session B"

        compiler = ContextCompiler(
            personas_service=_E2EPersonas(),
            instance_locator=lambda _owner, companion, _conversation: (
                companion,
                "genome-e2e",
            ),
            history_manager=HistoryManager(),
            memory_port=port,
            memory_timeout_s=4.0,
            active_commitment_limit=1,
            active_commitment_timeout_s=0.5,
            context_budget_mode="disabled",
        )
        turn = _turn_input(
            owner_id=OWNER_USER_ID,
            companion_id=COMPANION_ID,
            memory_realm_id=MEMORY_SPACE_ID,
            turn_id=f"recall-{marker}",
            text=f"书房那盆绿植叫什么名字？{marker}",
            session_id=session_b,
            conversation_id="brain-conversation-stable-across-reentry",
            input_modality="voice",
        )
        messages = await compiler.compile(turn)
        assert marker in messages[0].content
        assert turn.metadata["memory_trace"]["context_injected"] is True
    finally:
        await port.close()
        await bus.close()


async def test_agent_recall_latency_distribution(live_agent_runner) -> None:
    """Measure the single public Agent recall path without phrase classification."""
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
    pool = McpClientPool(routes=routes)
    port = EidolonMemoryPort(pool=pool)
    marker = f"火龙果-{uuid.uuid4().hex[:8]}"
    owner_id = "owner-perf"
    companion_id = "companion-perf"
    # The question must not contain the answer marker.  Including it would
    # turn this into an exact-token readback probe and hide the ordinary
    # product case: a person asks naturally for a fact they stated earlier.
    query = "我最喜欢的水果是什么？"
    try:
        write_started = time.perf_counter()
        await nats_publish_assertion(
            handle.nats_url,
            user_id=handle.user_id,
            text=f"owner likes {marker}",
            wing="Wing_Life",
            request_id=f"seed-{marker}",
            # Canonical owner facts use the graph's public ``self`` entity;
            # ContextCompiler queries that same authenticated focus subject.
            subject="self",
            predicate="likes",
            object_value=marker,
            companion_id=companion_id,
        )

        latest_recall = None

        async def _fact_visible() -> bool:
            nonlocal latest_recall
            latest_recall = await port.recall_context(
                owner_id,
                query,
                memory_realm_id=handle.user_id,
                plan=MemoryQueryPlan(
                    semantic_k=5,
                    voice=True,
                    kg_subjects=("self",),
                ),
                timeout_s=4.0,
                companion_id=companion_id,
                device_id="device-perf",
                session_id="session-perf",
            )
            return not latest_recall.degraded and marker in latest_recall.context

        assert await _wait_for_true(
            _fact_visible,
            timeout_s=30,
            poll_interval_s=0.05,
        ), latest_recall
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
            assert trace["timeout_ms"] == 500
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
            name: _latency_percentiles([sample[name] for sample in samples if name in sample])
            for name in sorted({key for sample in samples for key in sample})
        }
        report = {
            "write_visibility_ms": round(write_visibility_ms, 3),
            "query": query,
            "recall_budget_ms": 500,
            "metrics": metrics,
        }
        print("AGENT_MEMORY_PERF=" + json.dumps(report, ensure_ascii=False, sort_keys=True))

        assert metrics["agent_memory_elapsed_ms"]["p95"] < 500
        assert metrics["compiler_total_ms"]["p95"] < 500
    finally:
        await port.close()


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
    pool = McpClientPool(routes=routes)
    port = EidolonMemoryPort(pool=pool)
    marker = f"agent-commitment-context-{uuid.uuid4().hex[:8]}"
    later_marker = f"agent-commitment-later-{uuid.uuid4().hex[:8]}"
    owner_id = "owner-e2e"
    companion_id = "companion-e2e"
    try:
        request_id = await nats_publish_commitment(
            handle.nats_url,
            user_id=handle.user_id,
            subject="小忆",
            action=f"周六陪 owner 去恐龙园 {marker}",
            text=f"我答应周六陪你去恐龙园 {marker}",
            operation_hint="confirm",
            request_id=f"create-{marker}",
            beneficiaries=[owner_id],
            participants=["朋友甲", "朋友乙"],
            due_at="2026-07-18T09:00:00+08:00",
            status="confirmed",
            companion_id=companion_id,
        )
        assert request_id
        later_request_id = await nats_publish_commitment(
            handle.nats_url,
            user_id=handle.user_id,
            subject="小忆",
            action=f"下个月陪 owner 去博物馆 {later_marker}",
            text=f"我答应下个月陪你去博物馆 {later_marker}",
            operation_hint="confirm",
            request_id=f"create-{later_marker}",
            beneficiaries=[owner_id],
            due_at="2026-08-18T09:00:00+08:00",
            status="confirmed",
            companion_id=companion_id,
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
                (item for item in result.commitments if marker in item.action),
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

        fulfil_request_id = await nats_publish_commitment(
            handle.nats_url,
            user_id=handle.user_id,
            subject="小忆",
            action=current.action,
            text=f"我们已经去过恐龙园了 {marker}",
            operation_hint="update",
            request_id=f"fulfil-{marker}",
            target_id=current.commitment_id,
            status="fulfilled",
            companion_id=companion_id,
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
