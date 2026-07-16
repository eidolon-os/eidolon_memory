"""Canonical intent translation stays deterministic and non-reconciling."""

from __future__ import annotations

from eidolon.memory.application.memory_intents import memory_intents_from_decision
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
from eidolon.memory.domain.steward import PrivacyAction, StewardDecision

MEMORY_SPACE_ID = "r:alice:default"


def _fragment(memory_type: str, content: str) -> MemoryFragment:
    return MemoryFragment(
        memory_space_id=MEMORY_SPACE_ID,
        source_turn_id="turn-1",
        wing="Wing_Life",
        room="room",
        content=content,
        memory_type=memory_type,
        importance=4,
        confidence=0.9,
    )


def test_decision_maps_each_action_to_an_independent_deterministic_intent() -> None:
    decision = StewardDecision(
        should_write=True,
        fragments=[_fragment("preference", "用户喜欢绿茶")],
        triples=[
            KgTripleAction(
                subject="user",
                predicate="committed_to",
                object="带狗去恐龙园",
                confidence=0.95,
            )
        ],
        invalidations=[
            KgInvalidationAction(
                subject="user",
                predicate="works_at",
                object="常州",
                reason="用户明确纠正",
            )
        ],
        privacy_actions=[
            PrivacyAction(
                action="delete_request",
                target="旧住址",
                reason="用户要求删除",
            )
        ],
    )

    first = memory_intents_from_decision(
        decision,
        memory_space_id=MEMORY_SPACE_ID,
        source_event_id="turn-1",
    )
    second = memory_intents_from_decision(
        decision,
        memory_space_id=MEMORY_SPACE_ID,
        source_event_id="turn-1",
    )

    assert [item.intent_id for item in first] == [item.intent_id for item in second]
    assert len(set(item.intent_id for item in first)) == 4
    assert [item.intent_type for item in first] == [
        "preference",
        "commitment",
        "correction",
        "forget",
    ]
    assert [item.operation_hint for item in first] == [
        "add",
        "add",
        "invalidate",
        "invalidate",
    ]
    assert all(item.source_event_id == "turn-1" for item in first)
    assert first[1].subject == "user"
    assert first[1].predicate == "committed_to"


def test_goal_fragment_is_not_promoted_to_commitment_without_explicit_evidence() -> None:
    intents = memory_intents_from_decision(
        StewardDecision(
            should_write=True,
            fragments=[_fragment("goal", "用户想有一天去冰岛")],
        ),
        memory_space_id=MEMORY_SPACE_ID,
        source_event_id="turn-goal",
    )

    assert len(intents) == 1
    assert intents[0].intent_type == "fact"


def test_changed_extracted_claim_gets_a_different_intent_identity() -> None:
    green = memory_intents_from_decision(
        StewardDecision(
            should_write=True,
            fragments=[_fragment("preference", "用户喜欢绿茶")],
        ),
        memory_space_id=MEMORY_SPACE_ID,
        source_event_id="turn-1",
    )
    black = memory_intents_from_decision(
        StewardDecision(
            should_write=True,
            fragments=[_fragment("preference", "用户喜欢红茶")],
        ),
        memory_space_id=MEMORY_SPACE_ID,
        source_event_id="turn-1",
    )

    assert green[0].intent_id != black[0].intent_id
