from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from eidolon_sdk.memory import (
    ConversationTurnPayload,
    DeviceSyncBatchPayload,
    DeviceSyncEvent,
    MemoryActorContext,
    conversation_turn_subject,
    memory_command_subject,
    memory_sync_subject,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.application.steward.rules import RuleBasedSteward
from eidolon.memory.application.turn_processor import process_sync_message
from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.nats.names import memory_consumer_name, nats_safe_name
from eidolon.memory.infrastructure.sync_ledger import SyncLedger


def _ctx(device_id: str = "device-a", session_id: str = "session-a") -> MemoryActorContext:
    return MemoryActorContext(
        tenant_id="default",
        owner_user_id="alice",
        persona_id="mochi",
        agent_id="agent-mochi",
        device_id=device_id,
        instance_id=f"{device_id}-runtime",
        session_id=session_id,
    )


def _turn(
    text: str,
    *,
    device_id: str = "device-a",
    session_id: str = "session-a",
) -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id=uuid.uuid4().hex,
        context=_ctx(device_id=device_id, session_id=session_id),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        user_text=text,
        assistant_text="记下了。",
    )


def test_memory_space_subjects_are_new_contract() -> None:
    ctx = _ctx()
    assert ctx.memory_space_id == "default.alice.mochi"
    assert conversation_turn_subject(ctx.memory_space_id) == (
        "eidolon.memory.turn.b64_ZGVmYXVsdC5hbGljZS5tb2NoaQ"
    )
    assert memory_command_subject(ctx.memory_space_id) == (
        "eidolon.memory.cmd.b64_ZGVmYXVsdC5hbGljZS5tb2NoaQ"
    )
    assert memory_sync_subject(ctx.memory_space_id) == (
        "eidolon.memory.sync.b64_ZGVmYXVsdC5hbGljZS5tb2NoaQ"
    )


def test_dotted_memory_space_id_is_sanitized_for_jetstream_consumer_names() -> None:
    assert (
        memory_consumer_name("eidolon-memory-agent", "default.benchmark.default")
        == "eidolon-memory-agent-b64_ZGVmYXVsdC5iZW5jaG1hcmsuZGVmYXVsdA"
    )
    assert "." not in memory_consumer_name(
        "eidolon-memory-agent",
        "default.benchmark.default",
        role="sync",
    )
    assert nats_safe_name("default.benchmark.default") == "default_benchmark_default"


def test_fragment_extensions_validate_namespace() -> None:
    ctx = _ctx()
    fragment = MemoryFragment(
        memory_space_id=ctx.memory_space_id,
        scope="device",
        visibility="current_device",
        source_device_id=ctx.device_id,
        target_device_id=ctx.device_id,
        source_instance_id=ctx.instance_id,
        source_turn_id="turn-1",
        session_id=ctx.session_id,
        wing="Wing_Life",
        room="room",
        content="这台设备在客厅",
        memory_type="life",
        importance=4,
        confidence=0.8,
        extensions={"location": {"room": "客厅", "confidence": 0.9}},
    )
    assert fragment.extensions["location"]["room"] == "客厅"

    with pytest.raises(ValueError):
        MemoryFragment(
            memory_space_id=ctx.memory_space_id,
            scope="device",
            visibility="current_device",
            source_device_id=ctx.device_id,
            source_instance_id=ctx.instance_id,
            source_turn_id="turn-1",
            session_id=ctx.session_id,
            wing="Wing_Life",
            room="room",
            content="bad",
            memory_type="life",
            importance=4,
            confidence=0.8,
            extensions={"Location-Bad": {"room": "x"}},
        )


@pytest.mark.asyncio
async def test_backend_can_lookup_fragment_by_source_turn_id() -> None:
    ctx = _ctx()
    backend = FakeMemoryBackend()
    fragment = MemoryFragment(
        memory_space_id=ctx.memory_space_id,
        scope="persona",
        visibility="all_devices",
        source_device_id=ctx.device_id,
        source_instance_id=ctx.instance_id,
        source_turn_id="turn-exact-1",
        session_id=ctx.session_id,
        wing="Wing_Profile",
        room="preference",
        content="Alice likes oolong tea.",
        memory_type="profile",
        importance=4,
        confidence=0.9,
    )

    await backend.ingest_fragment(fragment)

    rec = await backend.get_by_source_turn_id(ctx.memory_space_id, "turn-exact-1")
    assert rec is not None
    assert rec.metadata["source_turn_id"] == "turn-exact-1"
    assert await backend.get_by_source_turn_id(ctx.memory_space_id, "missing") is None
    assert await backend.get_by_source_turn_id("default.bob.mochi", "turn-exact-1") is None


def test_recall_policy_keeps_other_device_out_of_rendered_context() -> None:
    ctx = _ctx(device_id="device-a")
    registry = RecallPolicyRegistry.default()
    current = MemoryWireRecord(
        memory_space_id=ctx.memory_space_id,
        key="current",
        value="这台设备在客厅",
        metadata={
            "memory_space_id": ctx.memory_space_id,
            "scope": "device",
            "visibility": "current_device",
            "source_device_id": "device-a",
            "extensions": json.dumps({"location": {"room": "客厅"}}),
        },
    )
    other = MemoryWireRecord(
        memory_space_id=ctx.memory_space_id,
        key="other",
        value="另一台设备在卧室",
        metadata={
            "memory_space_id": ctx.memory_space_id,
            "scope": "device",
            "visibility": "current_device",
            "source_device_id": "device-b",
        },
    )
    assert registry.visible(current, context=ctx)
    assert registry.rendered(current, context=ctx)
    assert not registry.visible(other, context=ctx)
    assert registry.rank([other, current], context=ctx, query="客厅", top_k=2) == [current]


@pytest.mark.asyncio
async def test_working_memory_partitions_by_device_and_session() -> None:
    import asyncio

    ring = WorkingMemoryRing(maxlen=5, lock=asyncio.Lock())
    await ring.append(_turn("A1", device_id="device-a", session_id="s1"))
    await ring.append(_turn("A2", device_id="device-a", session_id="s2"))
    await ring.append(_turn("B1", device_id="device-b", session_id="s1"))

    assert [t.user_text for t in await ring.snapshot(device_id="device-a", session_id="s1")] == [
        "A1"
    ]
    assert [t.user_text for t in await ring.snapshot(device_id="device-a", session_id="s2")] == [
        "A2"
    ]
    assert [t.user_text for t in await ring.snapshot(device_id="device-b", session_id="s1")] == [
        "B1"
    ]


@pytest.mark.asyncio
async def test_rules_steward_classifies_persona_and_device_memory() -> None:
    settings = load_memory_settings()
    steward = RuleBasedSteward(settings)

    persona = await steward.decide(_turn("我喜欢乌龙茶"))
    device = await steward.decide(_turn("这台设备在客厅，麦克风需要校准"))

    assert persona.fragments[0].scope == "persona"
    assert persona.fragments[0].visibility == "all_devices"
    assert device.fragments[0].scope == "device"
    assert device.fragments[0].visibility == "current_device"
    assert "location" in device.fragments[0].extensions
    assert "capability" in device.fragments[0].extensions


class _Msg:
    def __init__(self, payload: dict) -> None:
        self.data = json.dumps(payload).encode("utf-8")
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class _Backend:
    working_memory = None

    def __init__(self) -> None:
        self.fragments: list[MemoryFragment] = []

    async def ingest_fragment(self, fragment: MemoryFragment) -> None:
        self.fragments.append(fragment)

    async def delete(self, *_args, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_device_sync_batch_dedupes_events(tmp_path) -> None:
    settings = load_memory_settings()
    steward = RuleBasedSteward(settings)
    backend = _Backend()
    ledger = SyncLedger(tmp_path / "sync_ledger.sqlite3")
    turn = _turn("我喜欢乌龙茶").model_dump(mode="json")
    batch = DeviceSyncBatchPayload(
        request_id="sync-1",
        memory_space_id=_ctx().memory_space_id,
        issued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        issuer="agent",
        device_id="device-a",
        instance_id="device-a-runtime",
        events=[
            DeviceSyncEvent(
                event_id="event-1",
                idempotency_hash="hash-1",
                turn=turn,
            )
        ],
    )

    msg1 = _Msg(batch.model_dump(mode="json"))
    await process_sync_message(
        msg1,
        steward=steward,
        backend=backend,
        ledger=ledger,
        settings=settings,
        expected_memory_space_id=_ctx().memory_space_id,
    )
    msg2 = _Msg(batch.model_dump(mode="json"))
    await process_sync_message(
        msg2,
        steward=steward,
        backend=backend,
        ledger=ledger,
        settings=settings,
        expected_memory_space_id=_ctx().memory_space_id,
    )

    assert msg1.acked and msg2.acked
    assert len(backend.fragments) == 1
    assert ledger.seen(event_id="event-1", idempotency_hash="hash-1")
