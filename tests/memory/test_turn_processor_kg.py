"""T2: turn_processor + KG integration (G4 / G7 / G10 / replay idempotency)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eidolon_memory_contracts import envelope_memory_payload

from eidolon.memory.domain.space_lock import SpaceLock

# ─── Test fixtures ────────────────────────────────────────────────────────


SPACE_FOR_TESTS = "default.alice.default"


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
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    locked = SqliteKnowledgeGraph(
        tmp_path / "kg.sqlite3", space_id=SPACE_FOR_TESTS, lock=backend.lock
    )
    yield locked
    locked.close()


@pytest.fixture
def canonical(tmp_path: Path):
    from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger

    return CanonicalFactLedger(tmp_path / "canonical.sqlite3")


MEMORY_SPACE_ID = "r:alice:default"


def _turn_payload(
    *,
    memory_space_id: str = MEMORY_SPACE_ID,
    turn_id: str | None = None,
    **kwargs,
) -> dict:
    return {
        "turn_id": turn_id or uuid.uuid4().hex,
        "context": {
            "owner_id": kwargs.get("owner_id", "alice"),
            "companion_id": kwargs.get("companion_id", "test"),
            "memory_realm_id": memory_space_id,
            "device_id": kwargs.get("device_id", "device"),
            "session_id": kwargs.get("session_id", "s1"),
        },
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

    envelope = envelope_memory_payload(payload, kind=payload.get("kind", "conversation_turn"))
    return SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        ack=_ack,
        nak=_nak,
        ack_calls=ack_calls,
        nak_calls=nak_calls,
        metadata=SimpleNamespace(num_delivered=1),
    )


def _make_steward(decision):
    s = MagicMock()
    s.extraction_version = "test:v1"
    s.decide = AsyncMock(return_value=decision)
    return s


# ─── G7: KG write failure does not block ack ─────────────────────────────


async def test_kg_failure_leaves_projection_pending_and_naks(settings, backend, canonical):
    """A canonical drawer cannot be advertised complete while KG is absent."""
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    bad_kg = MagicMock()
    bad_kg.lock = backend.lock
    bad_kg.add_triple = AsyncMock(side_effect=RuntimeError("kg disk full"))
    bad_kg.invalidate = AsyncMock(return_value=0)

    fragment = MemoryFragment(
        memory_id="f1",
        memory_space_id="wrong.realm",
        source_device_id="wrong-device",
        source_instance_id="wrong-companion",
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
        subject="self",
        predicate="likes",
        object="tea",
        confidence=0.9,
    )
    decision = StewardDecision(
        should_write=True,
        reason="ok",
        fragments=[fragment],
        triples=[triple],
        invalidations=[],
    )

    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg,
        steward=_make_steward(decision),
        backend=backend,
        kg=bad_kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )
    assert msg.ack_calls == []
    assert msg.nak_calls == ["nak"]
    # The deterministic canonical drawer landed; redelivery will repair KG.
    rows = await backend.get_all("")
    assert any("self likes tea" in (r.value or "") for r in rows)
    row = next(r for r in rows if "self likes tea" in (r.value or ""))
    assert row.metadata["owner_id"] == "alice"
    assert row.metadata["companion_id"] == "test"
    assert row.metadata["memory_realm_id"] == MEMORY_SPACE_ID
    assert row.metadata["source_device_id"] == "device"
    assert row.metadata["source_instance_id"] == "test"


# ─── G7: chroma failure NAKs (fragment is source of truth) ────────────────


async def test_chroma_failure_naks_below_max_deliveries(settings, kg, canonical):
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.steward import StewardDecision

    bad_backend = MagicMock()
    bad_backend.lock = SpaceLock()
    bad_backend.ingest_fragment = AsyncMock(side_effect=RuntimeError("chroma corrupt"))
    bad_backend.delete = AsyncMock()
    fragment = MemoryFragment(
        memory_id="f1",
        memory_space_id=MEMORY_SPACE_ID,
        source_device_id="device",
        source_instance_id="instance",
        wing="Wing_Profile",
        room="r",
        content="x",
        memory_type="preference",
        importance=4,
        confidence=0.9,
        source_turn_id="t1",
        session_id="s1",
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
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )
    assert msg.nak_calls == ["nak"]
    assert msg.ack_calls == []


# ─── G1: same turn replayed twice → KG triple not duplicated ─────────────


async def test_replay_of_same_turn_does_not_duplicate_triples(settings, backend, kg, canonical):
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    triple = KgTripleAction(subject="self", predicate="likes", object="tea", confidence=0.9)
    decision = StewardDecision(
        should_write=True,
        reason="",
        triples=[triple],
    )
    steward = _make_steward(decision)

    payload = _turn_payload(turn_id="dedup-1")
    for _ in range(3):
        msg = _stub_msg(payload)
        await process_turn_message(
            msg,
            steward=steward,
            backend=backend,
            kg=kg,
            settings=settings,
            max_deliveries=3,
            expected_memory_space_id=MEMORY_SPACE_ID,
            canonical_facts=canonical,
        )
        assert msg.ack_calls == ["ack"]

    stats = await kg.stats()
    assert stats["triples_total"] == 1


# ─── G10: low-confidence triples dropped ─────────────────────────────────


async def test_low_confidence_triples_skipped(settings, backend, kg, canonical):
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    decision = StewardDecision(
        should_write=True,
        reason="",
        triples=[
            KgTripleAction(subject="self", predicate="likes", object="tea", confidence=0.9),
            KgTripleAction(
                subject="self", predicate="likes", object="wine", confidence=0.4
            ),  # < 0.6
        ],
    )
    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg,
        steward=_make_steward(decision),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )
    stats = await kg.stats()
    assert stats["triples_total"] == 1
    rows = await kg.query_entity("self", audiences=("companion:test",))
    assert {r.object for r in rows} == {"tea"}


# ─── Invalidation runs before any new triple is recorded ─────────────────


async def test_invalidation_applies_before_new_triple(settings, backend, kg, canonical):
    """If both an invalidation and a new triple target the same (s,p,o),
    the invalidation should fire first so we end up with one *new* triple
    with valid_to=None and an older one with valid_to set."""
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    # Seed an existing "likes coffee" via a prior turn.
    seed_decision = StewardDecision(
        should_write=True,
        reason="",
        triples=[
            KgTripleAction(subject="self", predicate="likes", object="coffee", confidence=0.95)
        ],
    )
    msg1 = _stub_msg(_turn_payload(turn_id="seed"))
    await process_turn_message(
        msg1,
        steward=_make_steward(seed_decision),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )

    # New turn changes mind.
    change_decision = StewardDecision(
        should_write=True,
        reason="",
        triples=[KgTripleAction(subject="self", predicate="likes", object="tea", confidence=0.95)],
        invalidations=[KgInvalidationAction(subject="self", predicate="likes", object="coffee")],
    )
    msg2 = _stub_msg(_turn_payload(turn_id="change"))
    await process_turn_message(
        msg2,
        steward=_make_steward(change_decision),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )

    coffee = [
        r
        for r in await kg.query_entity("self", audiences=("companion:test",))
        if r.object == "coffee"
    ]
    tea = [
        r for r in await kg.query_entity("self", audiences=("companion:test",)) if r.object == "tea"
    ]
    # Coffee invalidated → no current "likes coffee"
    assert not coffee
    # Tea is currently liked
    assert tea


# ─── Privacy turn writes nothing into KG ─────────────────────────────────


async def test_privacy_actions_skip_kg(settings, backend, kg, canonical, tmp_path):
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.steward import PrivacyAction, StewardDecision
    from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger

    decision = StewardDecision(
        should_write=False,
        reason="user said don't record",
        fragments=[],
        triples=[],
        invalidations=[],
        privacy_actions=[
            PrivacyAction(action="do_not_store", target="recent topic", reason="user request"),
        ],
    )
    msg = _stub_msg(_turn_payload(turn_id="privacy-do-not-store"))
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    await process_turn_message(
        msg,
        steward=_make_steward(decision),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
        decision_store=decisions,
    )
    stats = await kg.stats()
    assert stats["triples_total"] == 0
    assert msg.ack_calls == ["ack"]
    redacted = await decisions.get(
        MEMORY_SPACE_ID,
        "privacy-do-not-store",
        "test:v1",
    )
    assert redacted is not None and redacted.redacted is True


# ─── G10: pydantic Literal rejects bad predicate in StewardDecision ──────


async def test_decision_with_bad_predicate_rejected_at_pydantic() -> None:
    from pydantic import ValidationError

    from eidolon.memory.domain.steward import StewardDecision

    raw = {
        "should_write": True,
        "reason": "test",
        "fragments": [],
        "triples": [
            {
                "subject": "self",
                "predicate": "loves",  # not in whitelist
                "object": "x",
                "confidence": 0.9,
            }
        ],
        "invalidations": [],
        "privacy_actions": [],
    }
    with pytest.raises(ValidationError):
        StewardDecision.model_validate(raw)


# ─── KG-V13 G2: KG plan privacy carry-over via steward output ────────────


async def test_steward_output_with_health_predicate_propagates(settings, backend, kg, canonical):
    """Sanity: a health triple does land in KG, but is filtered from default reads."""
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    decision = StewardDecision(
        should_write=True,
        reason="",
        triples=[
            KgTripleAction(
                subject="self",
                predicate="has_health_condition",
                object="anxiety",
                confidence=0.95,
            )
        ],
    )
    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg,
        steward=_make_steward(decision),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )

    # default query (read-side) excludes sensitive predicates (KG plan §3.3 G2)
    default = await kg.query_entity("self", audiences=("companion:test",))
    assert not any(r.predicate == "has_health_condition" for r in default)
    # Sensitive interaction facts remain private to the companion that learned
    # them; opting in to sensitive data must not widen the audience to Owner.
    owner_opt_in = await kg.query_entity("self", audiences=("owner",), include_sensitive=True)
    assert not any(r.predicate == "has_health_condition" for r in owner_opt_in)
    opt_in = await kg.query_entity("self", audiences=("companion:test",), include_sensitive=True)
    assert any(r.predicate == "has_health_condition" for r in opt_in)


# ─── should_write is fragment-scoped, not turn-scoped ────────────────────


async def test_a_triple_survives_a_turn_whose_fragments_were_not_worth_keeping(
    settings, backend, kg, canonical
):
    """``should_write=False`` must not silence the graph.

    Read as a global gate it looks like a defect that the graph writes anyway,
    and it was filed as one. It is not: the LLM steward sets this flag from
    whether *fragments* survived importance filtering, and fragments and triples
    have separate thresholds because they are separate judgements. A fact too
    ordinary to keep as a memory can still be a relation worth knowing, which is
    the graph's whole reason to exist alongside the vector store.
    """

    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    decision = StewardDecision(
        should_write=False,
        reason="内容信号较弱，低于最小写入重要性阈值。",
        fragments=[],
        triples=[KgTripleAction(subject="用户", predicate="likes", object="绿茶", confidence=0.9)],
    )
    msg = _stub_msg(_turn_payload(turn_id="low-importance-1"))

    await process_turn_message(
        msg,
        steward=_make_steward(decision),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )

    assert msg.ack_calls == ["ack"]
    assert (await kg.stats())["triples_total"] == 1


async def test_a_correction_is_applied_even_with_nothing_worth_storing(
    settings, backend, kg, canonical
):
    """The reason gating on ``should_write`` would be worse than the bug it looks like.

    Someone saying "不对，我妈搬到北京了" is correcting a fact. Whether the same
    turn also yields a fragment worth keeping is unrelated — and dropping the
    correction leaves the superseded fact recallable, silently, which is the
    failure the whole invalidation path exists to prevent.
    """

    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
    from eidolon.memory.domain.steward import StewardDecision

    await process_turn_message(
        _stub_msg(_turn_payload(turn_id="the-old-fact")),
        steward=_make_steward(
            StewardDecision(
                should_write=True,
                reason="",
                triples=[
                    KgTripleAction(
                        subject="妈妈", predicate="lives_in", object="杭州", confidence=0.95
                    )
                ],
            )
        ),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )
    assert (await kg.stats())["triples_active"] == 1

    await process_turn_message(
        _stub_msg(_turn_payload(turn_id="the-correction")),
        steward=_make_steward(
            StewardDecision(
                should_write=False,
                reason="没有值得单独记住的片段。",
                fragments=[],
                invalidations=[
                    KgInvalidationAction(subject="妈妈", predicate="lives_in", object="杭州")
                ],
            )
        ),
        backend=backend,
        kg=kg,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )

    stats = await kg.stats()
    assert stats["triples_active"] == 0, "the correction was ignored"
    assert stats["triples_invalidated"] == 1
