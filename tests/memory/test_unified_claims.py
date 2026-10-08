"""Atomic extraction shares existing assertion identities and lifecycle ports."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_memory_contracts import (
    ConversationTurnPayload,
    build_memory_actor_context,
    envelope_memory_payload,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.forget import forget_exact_projections
from eidolon.memory.application.memory_intents import _intent_id, memory_intents_from_decision
from eidolon.memory.application.steward.llm import LiteLLMSteward
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.errors import StewardOutputError
from eidolon.memory.domain.kg import KgTripleAction
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger

SPACE = "r:claims:test"
TEXT = "我喜欢乌龙茶。张丽炖了红烧肉。"


def claim(content, fact=None, **extra):
    return dict(
        wing="Wing_Event",
        room="drawer_event",
        content=content,
        evidence_quote=content,
        memory_type="event",
        importance=4,
        confidence=0.95,
        fact=fact,
        **extra,
    )


def turn(text=TEXT, turn_id="mixed"):
    return ConversationTurnPayload(
        turn_id=turn_id,
        timestamp="2026-10-08T10:00:00Z",
        user_text=text,
        assistant_text="",
        context=build_memory_actor_context(
            owner_id="test",
            companion_id="test",
            memory_realm_id=SPACE,
            device_id="test",
            session_id="test",
        ),
    )


def steward(body):
    settings = load_memory_settings()
    settings.llm.model = "openai/local"
    completion = AsyncMock(
        return_value={"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}]}
    )
    return LiteLLMSteward(settings, completion=completion), completion


def test_historical_triple_intent_identity_is_unchanged():
    triple = KgTripleAction(subject="self", predicate="likes", object="乌龙茶")
    payload = triple.model_dump(mode="json", exclude={"statement", "statement_privacy"})
    expected = _intent_id(SPACE, "old", "triple", 0, payload)
    intents = memory_intents_from_decision(
        StewardDecision(should_write=True, triples=[triple]),
        memory_space_id=SPACE,
        source_event_id="old",
    )
    assert intents[0].intent_id == expected


@pytest.mark.asyncio
async def test_one_claim_has_one_intent_and_retains_user_details():
    body = {
        "should_write": True,
        "claims": [
            claim(
                "我妈张丽失眠一周了",
                {"subject": "mother", "predicate": "has_state", "object": "失眠"},
            )
        ],
    }
    service, _ = steward(body)
    decision = await service.decide(turn("我妈张丽失眠一周了"))
    intents = memory_intents_from_decision(decision, memory_space_id=SPACE, source_event_id="mixed")
    assert decision.unified_claims
    assert not decision.fragments
    assert len(intents) == 1
    assert intents[0].raw_claim == "我妈张丽失眠一周了"
    assert (intents[0].subject, intents[0].predicate, intents[0].object) == (
        "mother",
        "has_state",
        "失眠",
    )


@pytest.mark.parametrize(
    "body",
    [
        {"claims": None},
        {"claims": [], "fragments": {}},
        {"triples": "invalid"},
        {
            "claims": [
                claim(
                    "我喜欢乌龙茶",
                    {"subject": "self", "predicate": "likes", "object": "乌龙茶"},
                    scope="device",
                )
            ]
        },
        {"claims": [], "triples": [{"subject": "self", "predicate": "likes", "object": "茶"}]},
        {
            "claims": [
                claim(
                    "我很焦虑",
                    {"subject": "self", "predicate": "has_emotion", "object": "焦虑"},
                    privacy="sensitive",
                )
            ]
        },
    ],
)
@pytest.mark.asyncio
async def test_ambiguous_or_unsafe_claim_format_is_rejected(body):
    service, _ = steward({"should_write": True, **body})
    with pytest.raises(StewardOutputError):
        await service.decide(turn("我很焦虑"))


@pytest.mark.asyncio
async def test_model_cannot_enable_unified_writes_for_legacy_arrays():
    service, _ = steward({"should_write": False, "unified_claims": True})
    assert not (await service.decide(turn())).unified_claims


@pytest.mark.asyncio
async def test_mixed_claims_correct_delete_and_replay_through_existing_ports(tmp_path):
    body = {
        "should_write": True,
        "claims": [
            claim("我喜欢乌龙茶", {"subject": "self", "predicate": "likes", "object": "乌龙茶"}),
            claim("张丽炖了红烧肉"),
        ],
    }
    service, completion = steward(body)
    backend = LockedBackend(FakeMemoryBackend())
    kg = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE, lock=backend.lock)
    ledger = CanonicalFactLedger(tmp_path / "facts.sqlite3")
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")

    async def deliver(payload, extractor):
        msg = SimpleNamespace(
            data=json.dumps(
                envelope_memory_payload(
                    payload.model_dump(mode="json"), kind="conversation_turn"
                ).model_dump(mode="json")
            ).encode(),
            ack=AsyncMock(),
            nak=AsyncMock(),
            metadata=SimpleNamespace(num_delivered=1),
        )
        await process_turn_message(
            msg,
            steward=extractor,
            backend=backend,
            kg=kg,
            settings=load_memory_settings(),
            max_deliveries=3,
            expected_memory_space_id=SPACE,
            canonical_facts=ledger,
            decision_store=decisions,
        )
        msg.ack.assert_awaited_once()
        msg.nak.assert_not_awaited()

    try:
        await deliver(turn(), service)
        await deliver(turn(), service)
        assert completion.await_count == 1
        assert sorted(r.value for r in backend.inner.docs.values()) == [
            "张丽炖了红烧肉",
            "我喜欢乌龙茶",
        ]
        stats = await ledger.stats()
        assert stats.assertions_total == 2 and stats.evidence_total == 2
        correction, _ = steward(
            {
                "should_write": False,
                "claims": [],
                "invalidations": [
                    {
                        "subject": "self",
                        "predicate": "likes",
                        "object": "乌龙茶",
                        "evidence_quote": "我不再喜欢乌龙茶了",
                    }
                ],
            }
        )
        await deliver(turn("我不再喜欢乌龙茶了", "correct"), correction)
        active = [
            r for r in backend.inner.docs.values() if r.metadata.get("privacy") != "do_not_recall"
        ]
        assert [r.value for r in active] == ["张丽炖了红烧肉"]
        await forget_exact_projections(
            backend, kg, ledger, SPACE, [active[0].key], hard=True, decision_store=decisions
        )
        await deliver(turn(), service)
        assert completion.await_count == 1
        assert not any("红烧肉" in r.value for r in backend.inner.docs.values())
        active = [
            r for r in backend.inner.docs.values() if r.metadata.get("privacy") != "do_not_recall"
        ]
        assert active == []
    finally:
        kg.close()


def test_user_prompt_uses_the_same_claim_contract_as_the_system_prompt():
    service, _ = steward({})
    rendered = service._render_user_prompt(turn())
    assert "每个 claims[] 必须包含" in rendered
    assert "fragments" not in rendered
    assert "triples" not in rendered


@pytest.mark.asyncio
async def test_structured_claim_without_kg_is_retried_not_silently_dropped(tmp_path):
    service, _ = steward(
        {
            "should_write": True,
            "claims": [
                claim("我喜欢乌龙茶", {"subject": "self", "predicate": "likes", "object": "乌龙茶"})
            ],
        }
    )
    backend = LockedBackend(FakeMemoryBackend())
    ledger = CanonicalFactLedger(tmp_path / "facts.sqlite3")
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    msg = SimpleNamespace(
        data=json.dumps(
            envelope_memory_payload(
                turn().model_dump(mode="json"), kind="conversation_turn"
            ).model_dump(mode="json")
        ).encode(),
        ack=AsyncMock(),
        nak=AsyncMock(),
        metadata=SimpleNamespace(num_delivered=1),
    )
    await process_turn_message(
        msg,
        steward=service,
        backend=backend,
        kg=None,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=SPACE,
        canonical_facts=ledger,
        decision_store=decisions,
    )
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    assert not backend.inner.docs
    assert (await ledger.stats()).assertions_total == 0


@pytest.mark.asyncio
async def test_graph_disabled_keeps_claim_as_one_text_assertion():
    service, _ = steward(
        {
            "should_write": True,
            "claims": [
                claim("我喜欢乌龙茶", {"subject": "self", "predicate": "likes", "object": "乌龙茶"})
            ],
        }
    )
    service._settings.kg.backend = "none"
    decision = await service.decide(turn())
    assert not decision.triples
    assert len(decision.fragments) == 1
    assert decision.fragments[0].content == "我喜欢乌龙茶"
    intents = memory_intents_from_decision(decision, memory_space_id=SPACE, source_event_id="mixed")
    assert len(intents) == 1
    assert intents[0].raw_claim == "我喜欢乌龙茶"
