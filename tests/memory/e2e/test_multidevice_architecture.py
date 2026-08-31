from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from eidolon_memory_contracts import (
    ConversationTurnPayload,
    MemoryActorContext,
    build_memory_actor_context,
    envelope_memory_payload,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.public_recall import search_all_wings_mcp_style
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger


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


class _ScopeSteward:
    extraction_version = "test:scope"

    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        device_scoped = "麦克风" in turn.user_text
        return StewardDecision(
            should_write=True,
            fragments=[
                MemoryFragment(
                    memory_space_id=turn.context.memory_space_id,
                    source_turn_id=turn.turn_id,
                    scope="device" if device_scoped else "persona",
                    visibility="current_device" if device_scoped else "all_devices",
                    wing="Wing_Life",
                    room="device" if device_scoped else "preference",
                    content=turn.user_text,
                    evidence_quote=turn.user_text,
                    memory_type="device" if device_scoped else "preference",
                    importance=4,
                    confidence=0.9,
                )
            ],
        )


@pytest.mark.asyncio
async def test_persona_shared_but_device_memory_stays_current_device_only(tmp_path) -> None:
    settings = load_memory_settings()
    backend = FakeMemoryBackend()

    steward = _ScopeSteward()
    memory_space_id = _ctx("device-a").memory_space_id
    canonical_facts = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

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
            canonical_facts=canonical_facts,
        )
        assert msg.acked and not msg.nacked

    async def _recall(query: str, device_id: str) -> str:
        records = await search_all_wings_mcp_style(
            backend,
            settings,
            query=query,
            context=_ctx(device_id),
            top_k=10,
            wing=None,
            room=None,
        )
        return group_recall_context(records)

    assert "乌龙茶" in await _recall("乌龙茶", "device-b")
    assert "麦克风需要校准" not in await _recall("麦克风", "device-b")
    assert "乌龙茶" in await _recall("乌龙茶", "device-a")
    assert "麦克风需要校准" in await _recall("麦克风", "device-a")
