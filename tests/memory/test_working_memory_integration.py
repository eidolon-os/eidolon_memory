"""Phase 2 — functional tests covering the working-memory wire-up.

Three integration layers are exercised here without real NATS/MCP:

  * ``turn_processor.process_turn_message`` → ``backend.working_memory.append``
  * ``recall_with_kg_fusion`` → ``backend.working_memory.snapshot``
  * ``group_recall_context`` rendering [最近对话] before vector / KG

The corresponding e2e (real NATS + MCP) lives in
``tests/memory/e2e/test_working_memory_continuity.py``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.public_recall import recall_with_kg_fusion
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.domain.steward import StewardDecision

pytestmark = pytest.mark.asyncio


def _turn_msg(turn_id: str, user_text: str, assistant_text: str = "") -> SimpleNamespace:
    """Build a JetStream-shaped msg double that ``process_turn_message`` accepts."""
    payload = ConversationTurnPayload(
        turn_id=turn_id,
        user_text=user_text,
        assistant_text=assistant_text,
        timestamp="2026-05-25T00:00:00Z",
        session_id="unit",
        user_id="alice",
    ).model_dump()
    return SimpleNamespace(
        data=json.dumps(payload).encode("utf-8"),
        ack=AsyncMock(),
        metadata=SimpleNamespace(num_delivered=1),
    )


def _backend_with_ring(maxlen: int = 5) -> LockedBackend:
    backend = LockedBackend(FakeMemoryBackend())
    backend.working_memory = WorkingMemoryRing(maxlen=maxlen, lock=backend.lock)
    return backend


# ─── turn_processor → ring ─────────────────────────────────────────────────


async def test_turn_processor_appends_turn_to_ring():
    """W2 + W4 + Phase 2: every successfully-decoded turn ends up in the ring."""
    backend = _backend_with_ring()
    steward = SimpleNamespace(
        decide=AsyncMock(return_value=StewardDecision(should_write=False, fragments=[]))
    )
    settings = load_memory_settings()

    msg = _turn_msg("t1", "刚才说啥来着", "你问我了 X")
    await process_turn_message(
        msg, steward=steward, backend=backend, kg=None,
        settings=settings, max_deliveries=3, expected_user_id="alice",
    )

    snap = await backend.working_memory.snapshot()
    assert [t.turn_id for t in snap] == ["t1"]
    assert snap[0].user_text == "刚才说啥来着"


async def test_turn_processor_steward_failure_still_appends_to_ring():
    """Short-term continuity is independent of extraction quality."""
    backend = _backend_with_ring()

    async def _boom(_turn):
        raise RuntimeError("steward exploded")

    steward = SimpleNamespace(decide=AsyncMock(side_effect=_boom))
    settings = load_memory_settings()

    msg = _turn_msg("t-fail", "我说了点啥")
    # Steward failure → process_turn_message NAKs but the ring append happened
    # BEFORE steward.decide; verify the ring still holds the raw turn.
    try:
        await process_turn_message(
            msg, steward=steward, backend=backend, kg=None,
            settings=settings, max_deliveries=3, expected_user_id="alice",
        )
    except Exception:
        pass  # whatever the worker chose — we care about the ring state

    snap = await backend.working_memory.snapshot()
    assert [t.turn_id for t in snap] == ["t-fail"]


async def test_turn_processor_bad_payload_does_not_append():
    """JSON-broken messages must not poison the ring."""
    backend = _backend_with_ring()
    steward = SimpleNamespace(decide=AsyncMock())
    settings = load_memory_settings()

    bad_msg = SimpleNamespace(
        data=b"this is not json {{{",
        ack=AsyncMock(),
        metadata=SimpleNamespace(num_delivered=1),
    )
    await process_turn_message(
        bad_msg, steward=steward, backend=backend, kg=None,
        settings=settings, max_deliveries=3, expected_user_id="alice",
    )

    assert await backend.working_memory.snapshot() == []
    steward.decide.assert_not_called()


# ─── recall_with_kg_fusion → working_memory key ───────────────────────────


async def test_recall_fusion_returns_working_memory_snapshot():
    """recall_with_kg_fusion must surface ``working_memory`` in its result dict."""
    backend = _backend_with_ring()
    await backend.working_memory.append(
        ConversationTurnPayload(
            turn_id="t-recent", user_text="刚刚的事", assistant_text="嗯嗯",
            timestamp="2026-05-25T00:00:00Z", session_id="s", user_id="alice",
        )
    )
    settings = load_memory_settings()
    result = await recall_with_kg_fusion(
        backend, settings,
        query="任何查询", user_id="alice", top_k=3,
        kg=None, for_voice=False,
    )
    assert "working_memory" in result
    wm = result["working_memory"]
    assert len(wm) == 1
    assert wm[0].turn_id == "t-recent"


async def test_recall_fusion_returns_empty_working_memory_when_disabled():
    """Backend without a ring → empty list, no exception."""
    backend = LockedBackend(FakeMemoryBackend())  # no working_memory attr set
    settings = load_memory_settings()
    result = await recall_with_kg_fusion(
        backend, settings,
        query="任何", user_id="alice", top_k=3,
        kg=None, for_voice=False,
    )
    assert result["working_memory"] == []


# ─── renderer: [最近对话] section ordering ─────────────────────────────────


def _turn(i: int, user="u", asst="a") -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id=f"t-{i}", user_text=f"{user}-{i}", assistant_text=f"{asst}-{i}",
        timestamp="2026-05-25T00:00:00Z", session_id="s", user_id="alice",
    )


def test_renderer_working_memory_appears_first():
    """[最近对话] must precede every other section."""
    turns = [_turn(0, "你好", "你好啊"), _turn(1, "今天天气", "晴")]
    out = group_recall_context(records=[], kg_triples=None, working_memory=turns)
    assert out.startswith("[最近对话]"), out
    # Both verbatim turns appear, in order, oldest first.
    assert "用户: 你好-0" in out
    assert "你:   你好啊-0" in out
    assert "用户: 今天天气-1" in out
    # Ordering: user-0 before user-1.
    assert out.index("你好-0") < out.index("今天天气-1")


def test_renderer_empty_working_memory_omits_section():
    """No turns → no [最近对话] header (don't pollute LLM context)."""
    out = group_recall_context(records=[], kg_triples=None, working_memory=[])
    assert "[最近对话]" not in out


def test_renderer_caps_at_5_most_recent_turns():
    """``_WM_MAX_TURNS`` keeps the LLM context bounded."""
    turns = [_turn(i) for i in range(10)]
    out = group_recall_context(records=[], kg_triples=None, working_memory=turns)
    assert "u-9" in out                # most recent must survive
    assert "u-0" not in out            # oldest must be dropped
    # 5 turns × (user + assistant) = 10 dash-prefixed lines
    dash_lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(dash_lines) == 10, dash_lines


def test_renderer_truncates_long_turn_text():
    """A 1000-char user paste must not blow the LLM context."""
    big_turn = ConversationTurnPayload(
        turn_id="t-big", user_text="X" * 1000, assistant_text="Y",
        timestamp="2026-05-25T00:00:00Z", session_id="s", user_id="alice",
    )
    out = group_recall_context(records=[], kg_triples=None, working_memory=[big_turn])
    # Truncation marker present; full 1000-char string is NOT.
    assert "…" in out
    assert "X" * 1000 not in out


def test_renderer_working_memory_then_vector_then_kg_order():
    """Full stack: [最近对话] → vector sections → KG section."""
    from eidolon.memory.domain.kg import KgTripleRecord
    from eidolon.memory.domain.wire import MemoryWireRecord
    turns = [_turn(0, "继续", "好")]
    vector = [
        MemoryWireRecord(
            user_id="alice", key="k1", value="用户喜欢茶",
            metadata={"memory_type": "preference"},
        )
    ]
    triple = KgTripleRecord(
        id="t1", subject="self", predicate="likes",
        object="tea", valid_from=None, valid_to=None,
    )
    out = group_recall_context(records=vector, kg_triples=[triple], working_memory=turns)
    p_wm  = out.find("[最近对话]")
    p_vec = out.find("生活方式与近况")
    p_kg  = out.find("知识图谱")
    assert 0 == p_wm < p_vec < p_kg, (p_wm, p_vec, p_kg, out)
