from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from eidolon_sdk.memory import (
    ConversationTurnPayload,
    MemoryActorContext,
    build_memory_actor_context,
    envelope_memory_payload,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.public_recall import search_all_wings_mcp_style
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.application.steward.rules import RuleBasedSteward
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon.memory.config.memory_settings import load_memory_settings


def _ctx(device_id: str, session_id: str = "s") -> MemoryActorContext:
    return build_memory_actor_context(
        memory_realm_id="r:alice:mochi",
        owner_id="alice",
        companion_id="mochi",
        device_id=device_id,
        session_id=session_id,
    )


def _turn(text: str, *, device_id: str, session_id: str = "s") -> dict:
    payload = ConversationTurnPayload(
        turn_id=uuid.uuid4().hex,
        context=_ctx(device_id, session_id),
        timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        user_text=text,
        assistant_text="记下了。",
    )
    return envelope_memory_payload(payload, trace_id=payload.turn_id).model_dump(mode="json")


class _Msg:
    def __init__(self, payload: dict) -> None:
        self.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.acked = False
        self.nacked = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.nacked = True


@pytest.mark.asyncio
async def test_persona_shared_but_device_memory_stays_current_device_only() -> None:
    settings = load_memory_settings()
    backend = FakeMemoryBackend()
    import asyncio

    backend.working_memory = WorkingMemoryRing(maxlen=5, lock=asyncio.Lock())
    steward = RuleBasedSteward(settings)
    memory_space_id = _ctx("device-a").memory_space_id

    for payload in [
        _turn("我喜欢乌龙茶", device_id="device-a"),
        _turn("这台设备在客厅，麦克风需要校准", device_id="device-a"),
    ]:
        msg = _Msg(payload)
        await process_turn_message(
            msg,
            steward=steward,
            backend=backend,
            kg=None,
            settings=settings,
            max_deliveries=3,
            expected_memory_space_id=memory_space_id,
        )
        assert msg.acked and not msg.nacked

    device_b_records = await search_all_wings_mcp_style(
        backend,
        settings,
        query="用户",
        context=_ctx("device-b"),
        top_k=10,
        wing=None,
        room=None,
    )
    rendered_b = group_recall_context(device_b_records)
    assert "乌龙茶" in rendered_b
    assert "麦克风需要校准" not in rendered_b

    device_a_records = await search_all_wings_mcp_style(
        backend,
        settings,
        query="用户",
        context=_ctx("device-a"),
        top_k=10,
        wing=None,
        room=None,
    )
    rendered_a = group_recall_context(device_a_records)
    assert "乌龙茶" in rendered_a
    assert "麦克风需要校准" in rendered_a
