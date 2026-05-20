"""Cross-tier KG integration: turn → steward → worker → KG → recall (closed loop).

This file exercises T1 (LockedKnowledgeGraph + command path) + T2 (steward output
applied by worker) + T3 (recall_with_kg_fusion) wired end-to-end. The steward
is a stub returning a fixed ``StewardDecision`` so the test is deterministic
and avoids hitting a real LLM, but every other layer is the real production
code path.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stack(tmp_path: Path):
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.locked_backend import LockedBackend
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
    from eidolon.memory.config.memory_settings import load_memory_settings

    backend = LockedBackend(FakeMemoryBackend())
    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")),
        backend.lock,
    )
    settings = load_memory_settings()
    yield backend, kg, settings
    kg.close()


def _turn_payload(
    *,
    user_id: str = "alice",
    turn_id: str | None = None,
    user_text: str = "我喜欢喝茶",
    assistant_text: str = "好的，记住了",
    timestamp: str = "2026-05-19T10:00:00Z",
) -> dict:
    return {
        "turn_id": turn_id or uuid.uuid4().hex,
        "user_id": user_id,
        "session_id": "s1",
        "timestamp": timestamp,
        "user_text": user_text,
        "assistant_text": assistant_text,
    }


def _stub_msg(payload: dict) -> SimpleNamespace:
    async def _ack() -> None:
        msg.ack_calls.append("ack")

    async def _nak() -> None:
        msg.nak_calls.append("nak")

    msg = SimpleNamespace(
        data=json.dumps(payload).encode("utf-8"),
        ack_calls=[],
        nak_calls=[],
        metadata=SimpleNamespace(num_delivered=1),
    )
    msg.ack = _ack
    msg.nak = _nak
    return msg


def _stub_steward(decision):
    s = MagicMock()
    s.decide = AsyncMock(return_value=decision)
    return s


# ─── 1. Turn → steward → worker writes triple → recall surfaces KG ──────


async def test_turn_to_recall_closed_loop(stack) -> None:
    """A conversation turn whose steward extracts 'self likes tea' must
    surface as a KG triple in a subsequent ``recall_with_kg_fusion`` call.
    """
    from eidolon.memory.application.public_recall import (
        group_recall_context,
        recall_with_kg_fusion,
    )
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    backend, kg, settings = stack

    fragment = MemoryFragment(
        fragment_id="f1",
        user_id="alice",
        wing="Wing_Profile",
        room="profile_core",
        content="user likes tea",
        memory_type="preference",
        importance=4,
        confidence=0.95,
        source_turn_id="t1",
        session_id="s1",
    )
    triple = KgTripleAction(
        subject="self", predicate="likes", object="tea", confidence=0.92,
    )
    decision = StewardDecision(
        should_write=True,
        reason="user expressed preference",
        fragments=[fragment],
        triples=[triple],
    )

    msg = _stub_msg(_turn_payload(turn_id="turn-1"))
    await process_turn_message(
        msg,
        steward=_stub_steward(decision),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_user_id="alice",
    )
    assert msg.ack_calls == ["ack"]

    # entity cache must reflect the just-added "self"
    fused = await recall_with_kg_fusion(
        backend,
        settings,
        query="self likes tea",
        user_id="alice",
        top_k=5,
        kg=kg,
        for_voice=False,
    )
    assert any(
        t.predicate == "likes" and t.object == "tea" for t in fused["kg"]
    ), f"expected likes/tea in KG side, got {fused['kg']!r}"

    ctx = group_recall_context(fused["vector"], kg_triples=fused["kg"])
    # KG section must be present with Chinese transcription
    assert "知识图谱事实" in ctx
    assert "tea" in ctx
    assert "喜欢" in ctx


# ─── 2. Admin command path (NATS cmd) writes KG → visible to recall ──────


async def test_admin_command_to_recall_closed_loop(stack) -> None:
    """The admin / IDE path publishes ``KgAddTripleCommand`` (no steward).
    Worker applies it; subsequent recall picks it up identically.
    """
    from eidolon.memory.application.public_recall import recall_with_kg_fusion
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.domain.kg import KgAddTripleCommand

    backend, kg, settings = stack

    cmd = KgAddTripleCommand(
        request_id=uuid.uuid4().hex,
        user_id="alice",
        issued_at="2026-05-19T10:01:00Z",
        subject="self",
        predicate="practices",
        object="meditation",
        valid_from="2026-05-19T10:01:00Z",
        valid_to=None,
        confidence=0.99,
        source_drawer_id="req:admin",
        adapter_name="admin",
    )
    msg = _stub_msg(cmd.model_dump(mode="json"))
    await process_command_message(
        msg,
        backend=backend,
        kg=kg,
        settings=settings,
        expected_user_id="alice",
    )
    assert msg.ack_calls == ["ack"]
    fused = await recall_with_kg_fusion(
        backend, settings,
        query="self practices meditation",
        user_id="alice", top_k=5,
        kg=kg, for_voice=False,
    )
    assert any(
        t.predicate == "practices" and t.object == "meditation"
        for t in fused["kg"]
    )


# ─── 3. Change-of-mind across turns → recall sees current state only ─────


async def test_change_of_mind_invalidation_visible_via_recall(stack) -> None:
    """Turn 1: 'self likes coffee'. Turn 2: 'changed mind, likes tea' with
    invalidation. Recall (as_of NOW, default) must surface only 'tea'.
    """
    from eidolon.memory.application.public_recall import recall_with_kg_fusion
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    backend, kg, settings = stack

    # Seed: likes coffee
    seed_decision = StewardDecision(
        should_write=True, reason="seed",
        triples=[KgTripleAction(subject="self", predicate="likes",
                                object="coffee", confidence=0.95)],
    )
    msg1 = _stub_msg(_turn_payload(turn_id="seed", timestamp="2026-05-10T09:00:00Z"))
    await process_turn_message(
        msg1, steward=_stub_steward(seed_decision),
        backend=backend, kg=kg, settings=settings,
        max_deliveries=3, expected_user_id="alice",
    )
    assert msg1.ack_calls == ["ack"]

    # Change of mind
    change_decision = StewardDecision(
        should_write=True, reason="change",
        triples=[KgTripleAction(subject="self", predicate="likes",
                                object="tea", confidence=0.95)],
        invalidations=[KgInvalidationAction(
            subject="self", predicate="likes", object="coffee",
        )],
    )
    msg2 = _stub_msg(_turn_payload(turn_id="change",
                                   timestamp="2026-05-19T10:00:00Z"))
    await process_turn_message(
        msg2, steward=_stub_steward(change_decision),
        backend=backend, kg=kg, settings=settings,
        max_deliveries=3, expected_user_id="alice",
    )
    assert msg2.ack_calls == ["ack"]
    fused = await recall_with_kg_fusion(
        backend, settings,
        query="self likes",
        user_id="alice", top_k=5,
        kg=kg, for_voice=False,
    )
    objects = {(t.predicate, t.object) for t in fused["kg"]}
    assert ("likes", "tea") in objects
    assert ("likes", "coffee") not in objects, (
        f"invalidated triple should not appear in current recall, got {objects!r}"
    )


# ─── 4. Sensitive predicate writes land but stay hidden by default ───────


async def test_sensitive_predicate_hidden_from_default_recall(stack) -> None:
    """Steward output of a health predicate writes to KG, but the default
    recall fusion (include_sensitive_kg=False) must NOT surface it.
    """
    from eidolon.memory.application.public_recall import recall_with_kg_fusion
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    backend, kg, settings = stack

    decision = StewardDecision(
        should_write=True, reason="health",
        triples=[KgTripleAction(
            subject="self", predicate="has_health_condition",
            object="anxiety", confidence=0.97,
        )],
    )
    msg = _stub_msg(_turn_payload(turn_id="health"))
    await process_turn_message(
        msg, steward=_stub_steward(decision),
        backend=backend, kg=kg, settings=settings,
        max_deliveries=3, expected_user_id="alice",
    )
    assert msg.ack_calls == ["ack"]
    # default — sensitive filtered
    fused = await recall_with_kg_fusion(
        backend, settings,
        query="self anxiety",
        user_id="alice", top_k=5,
        kg=kg, for_voice=False,
        include_sensitive_kg=False,
    )
    assert not any(
        t.predicate == "has_health_condition" for t in fused["kg"]
    )

    # opt-in — surfaces
    fused2 = await recall_with_kg_fusion(
        backend, settings,
        query="self anxiety",
        user_id="alice", top_k=5,
        kg=kg, for_voice=False,
        include_sensitive_kg=True,
    )
    assert any(
        t.predicate == "has_health_condition" for t in fused2["kg"]
    )


# ─── 5. Replay safety: same turn twice does not double-write ─────────────


async def test_replay_does_not_duplicate_in_recall(stack) -> None:
    """Replay of the same turn (NATS at-least-once) → recall still surfaces
    one triple, never two.
    """
    from eidolon.memory.application.public_recall import recall_with_kg_fusion
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    backend, kg, settings = stack

    decision = StewardDecision(
        should_write=True, reason="",
        triples=[KgTripleAction(subject="self", predicate="practices",
                                object="yoga", confidence=0.9)],
    )
    payload = _turn_payload(turn_id="replay-1")
    steward = _stub_steward(decision)
    for _ in range(3):
        msg = _stub_msg(payload)
        await process_turn_message(
            msg, steward=steward,
            backend=backend, kg=kg, settings=settings,
            max_deliveries=3, expected_user_id="alice",
        )
        assert msg.ack_calls == ["ack"]
    fused = await recall_with_kg_fusion(
        backend, settings,
        query="self practices yoga",
        user_id="alice", top_k=5,
        kg=kg, for_voice=False,
    )
    yoga = [t for t in fused["kg"]
            if t.predicate == "practices" and t.object == "yoga"]
    assert len(yoga) == 1, f"expected exactly 1 yoga triple, got {len(yoga)}"
