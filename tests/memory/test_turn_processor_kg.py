"""T2: turn_processor + KG integration (G4 / G7 / G10 / replay idempotency)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ─── Test fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def settings():
    from eidolon.memory.config.memory_settings import load_memory_settings
    return load_memory_settings()


@pytest.fixture
def backend():
    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.locked_backend import LockedBackend
    return LockedBackend(FakeMemoryBackend())


@pytest.fixture
def kg(backend, tmp_path: Path):
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    inner = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
    locked = LockedKnowledgeGraph(inner, backend.lock)
    yield locked
    locked.close()


def _turn_payload(*, user_id: str = "alice", turn_id: str | None = None, **kwargs) -> dict:
    return {
        "turn_id": turn_id or uuid.uuid4().hex,
        "user_id": user_id,
        "session_id": kwargs.get("session_id", "s1"),
        "timestamp": kwargs.get("timestamp", "2026-05-19T10:00:00Z"),
        "user_text": kwargs.get("user_text", "hello"),
        "assistant_text": kwargs.get("assistant_text", "hi"),
    }


def _stub_msg(payload: dict) -> SimpleNamespace:
    ack_calls = []
    nak_calls = []

    async def _ack():
        ack_calls.append("ack")

    async def _nak():
        nak_calls.append("nak")

    return SimpleNamespace(
        data=json.dumps(payload).encode("utf-8"),
        ack=_ack,
        nak=_nak,
        ack_calls=ack_calls,
        nak_calls=nak_calls,
        metadata=SimpleNamespace(num_delivered=1),
    )


def _make_steward(decision):
    s = MagicMock()
    s.decide = AsyncMock(return_value=decision)
    return s


# ─── G7: KG write failure does not block ack ─────────────────────────────


async def test_kg_failure_does_not_block_chat_ack(settings, backend):
    """When KG raises, fragment write still succeeds and msg is acked."""
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    bad_kg = MagicMock()
    bad_kg.lock = backend.lock
    bad_kg.add_triple = AsyncMock(side_effect=RuntimeError("kg disk full"))
    bad_kg.invalidate = AsyncMock(return_value=0)

    fragment = MemoryFragment(
        fragment_id="f1", user_id="alice", wing="Wing_Profile", room="profile_core",
        content="user likes tea", memory_type="preference",
        importance=4, confidence=0.95,
        source_turn_id="t1", session_id="s1",
    )
    triple = KgTripleAction(
        subject="self", predicate="likes", object="tea", confidence=0.9,
    )
    decision = StewardDecision(
        should_write=True, reason="ok",
        fragments=[fragment], triples=[triple], invalidations=[],
    )

    msg = _stub_msg(_turn_payload(user_id="alice"))
    await process_turn_message(
        msg,
        steward=_make_steward(decision),
        backend=backend,
        kg=bad_kg,
        settings=settings,
        max_deliveries=3,
        expected_user_id="alice",
    )
    # ack despite KG failure
    assert msg.ack_calls == ["ack"]
    assert msg.nak_calls == []
    # fragment did make it
    rows = await backend.get_all("")
    assert any("user likes tea" in (r.value or "") for r in rows)


# ─── G7: chroma failure NAKs (fragment is source of truth) ────────────────


async def test_chroma_failure_naks_below_max_deliveries(settings, kg):
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.steward import StewardDecision

    bad_backend = MagicMock()
    bad_backend.lock = asyncio.Lock()
    bad_backend.ingest_fragment = AsyncMock(side_effect=RuntimeError("chroma corrupt"))
    bad_backend.delete = AsyncMock()
    fragment = MemoryFragment(
        fragment_id="f1", user_id="alice", wing="Wing_Profile", room="r",
        content="x", memory_type="preference", importance=4, confidence=0.9,
        source_turn_id="t1", session_id="s1",
    )
    decision = StewardDecision(should_write=True, reason="", fragments=[fragment])

    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg,
        steward=_make_steward(decision),
        backend=bad_backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_user_id="alice",
    )
    assert msg.nak_calls == ["nak"]
    assert msg.ack_calls == []


# ─── G1: same turn replayed twice → KG triple not duplicated ─────────────


async def test_replay_of_same_turn_does_not_duplicate_triples(settings, backend, kg):
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    triple = KgTripleAction(subject="self", predicate="likes", object="tea", confidence=0.9)
    decision = StewardDecision(
        should_write=True, reason="", triples=[triple],
    )
    steward = _make_steward(decision)

    payload = _turn_payload(turn_id="dedup-1")
    for _ in range(3):
        msg = _stub_msg(payload)
        await process_turn_message(
            msg, steward=steward, backend=backend, kg=kg,
            settings=settings, max_deliveries=3, expected_user_id="alice",
        )
        assert msg.ack_calls == ["ack"]

    stats = await kg.stats()
    assert stats["triples_total"] == 1


# ─── G10: low-confidence triples dropped ─────────────────────────────────


async def test_low_confidence_triples_skipped(settings, backend, kg):
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    decision = StewardDecision(
        should_write=True, reason="",
        triples=[
            KgTripleAction(subject="self", predicate="likes", object="tea", confidence=0.9),
            KgTripleAction(subject="self", predicate="likes", object="wine", confidence=0.4),  # < 0.6
        ],
    )
    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg, steward=_make_steward(decision), backend=backend, kg=kg,
        settings=settings, max_deliveries=3, expected_user_id="alice",
    )
    stats = await kg.stats()
    assert stats["triples_total"] == 1
    rows = await kg.query_entity("self")
    assert {r.object for r in rows} == {"tea"}


# ─── Invalidation runs before any new triple is recorded ─────────────────


async def test_invalidation_applies_before_new_triple(settings, backend, kg):
    """If both an invalidation and a new triple target the same (s,p,o),
    the invalidation should fire first so we end up with one *new* triple
    with valid_to=None and an older one with valid_to set."""
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    # Seed an existing "likes coffee" via a prior turn.
    seed_decision = StewardDecision(
        should_write=True, reason="",
        triples=[KgTripleAction(subject="self", predicate="likes", object="coffee", confidence=0.95)],
    )
    msg1 = _stub_msg(_turn_payload(turn_id="seed"))
    await process_turn_message(
        msg1, steward=_make_steward(seed_decision), backend=backend, kg=kg,
        settings=settings, max_deliveries=3, expected_user_id="alice",
    )

    # New turn changes mind.
    change_decision = StewardDecision(
        should_write=True, reason="",
        triples=[KgTripleAction(subject="self", predicate="likes", object="tea", confidence=0.95)],
        invalidations=[KgInvalidationAction(subject="self", predicate="likes", object="coffee")],
    )
    msg2 = _stub_msg(_turn_payload(turn_id="change"))
    await process_turn_message(
        msg2, steward=_make_steward(change_decision), backend=backend, kg=kg,
        settings=settings, max_deliveries=3, expected_user_id="alice",
    )

    coffee = [r for r in await kg.query_entity("self") if r.object == "coffee"]
    tea = [r for r in await kg.query_entity("self") if r.object == "tea"]
    # Coffee invalidated → no current "likes coffee"
    assert not coffee
    # Tea is currently liked
    assert tea


# ─── Privacy turn writes nothing into KG ─────────────────────────────────


async def test_privacy_actions_skip_kg(settings, backend, kg):
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.steward import PrivacyAction, StewardDecision

    decision = StewardDecision(
        should_write=False, reason="user said don't record",
        fragments=[], triples=[], invalidations=[],
        privacy_actions=[
            PrivacyAction(action="do_not_store", target="recent topic", reason="user request"),
        ],
    )
    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg, steward=_make_steward(decision), backend=backend, kg=kg,
        settings=settings, max_deliveries=3, expected_user_id="alice",
    )
    stats = await kg.stats()
    assert stats["triples_total"] == 0


# ─── G10: pydantic Literal rejects bad predicate in StewardDecision ──────


async def test_decision_with_bad_predicate_rejected_at_pydantic() -> None:
    from pydantic import ValidationError

    from eidolon.memory.domain.steward import StewardDecision

    raw = {
        "should_write": True,
        "reason": "test",
        "fragments": [],
        "triples": [{
            "subject": "self",
            "predicate": "loves",   # not in whitelist
            "object": "x",
            "confidence": 0.9,
        }],
        "invalidations": [],
        "privacy_actions": [],
    }
    with pytest.raises(ValidationError):
        StewardDecision.model_validate(raw)


# ─── KG-V13 G2: KG plan privacy carry-over via steward output ────────────


async def test_steward_output_with_health_predicate_propagates(settings, backend, kg):
    """Sanity: a health triple does land in KG, but is filtered from default reads."""
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    decision = StewardDecision(
        should_write=True, reason="",
        triples=[
            KgTripleAction(
                subject="self", predicate="has_health_condition", object="anxiety",
                confidence=0.95,
            )
        ],
    )
    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg, steward=_make_steward(decision), backend=backend, kg=kg,
        settings=settings, max_deliveries=3, expected_user_id="alice",
    )

    # default query (read-side) excludes sensitive predicates (KG plan §3.3 G2)
    default = await kg.query_entity("self")
    assert not any(r.predicate == "has_health_condition" for r in default)
    # opt-in returns them
    opt_in = await kg.query_entity("self", include_sensitive=True)
    assert any(r.predicate == "has_health_condition" for r in opt_in)
