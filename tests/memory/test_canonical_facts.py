"""Canonical exact facts retain confirmation evidence across projection retries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from eidolon_sdk.memory import MemoryIntent

from eidolon.memory.domain.canonical_fact import (
    CanonicalEvidenceConflict,
    CanonicalFactConflict,
    CanonicalFactInactive,
    canonical_assertion_id,
)
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger

MEMORY_SPACE_ID = "r:alice:default"


def _intent(
    intent_id: str,
    *,
    object_: str = "乌龙茶",
    source_event_id: str = "turn-1",
) -> MemoryIntent:
    return MemoryIntent(
        intent_id=intent_id,
        memory_space_id=MEMORY_SPACE_ID,
        source_event_id=source_event_id,
        authority="explicit_user",
        intent_type="preference",
        raw_claim=f"self likes {object_}",
        operation_hint="confirm",
        subject="self",
        predicate="likes",
        object=object_,
        tool_call_id=f"call:{intent_id}",
        confidence=0.99,
    )


def _invalidation(intent_id: str = "intent:invalidate-1") -> MemoryIntent:
    return MemoryIntent(
        intent_id=intent_id,
        memory_space_id=MEMORY_SPACE_ID,
        source_event_id="turn-invalidate",
        authority="extracted_user",
        intent_type="correction",
        raw_claim="invalidate self likes 乌龙茶",
        operation_hint="invalidate",
        subject="self",
        predicate="likes",
        object="乌龙茶",
        occurred_at="2026-06-02T00:00:00Z",
        confidence=1.0,
        attributes={"reason": "changed preference"},
    )


@pytest.mark.asyncio
async def test_projection_is_required_until_marked_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    intent = _intent("intent:1")

    first = await ledger.register(intent, targets={"drawer", "kg"})
    retry = await ledger.register(intent, targets={"drawer", "kg"})

    assert first.assertion_id == retry.assertion_id
    assert first.evidence_created is True
    assert retry.evidence_created is False
    assert retry.evidence_count == 1
    assert retry.pending_targets == ["drawer", "kg"]

    await ledger.mark_projected(
        MEMORY_SPACE_ID,
        first.assertion_id,
        targets={"drawer", "kg"},
    )
    reopened = CanonicalFactLedger(ledger.path)
    after_projection = await reopened.register(
        intent,
        targets={"drawer", "kg"},
    )
    assert after_projection.pending_targets == []
    assert after_projection.evidence_count == 1

    await reopened.mark_projection_pending(
        MEMORY_SPACE_ID,
        first.assertion_id,
        targets={"kg"},
    )
    pending_again = await reopened.register(intent, targets={"drawer", "kg"})
    assert pending_again.pending_targets == ["kg"]


@pytest.mark.asyncio
async def test_new_confirmation_keeps_one_fact_and_adds_provenance(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    first = await ledger.register(_intent("intent:1"), targets={"drawer", "kg"})
    await ledger.mark_projected(
        MEMORY_SPACE_ID,
        first.assertion_id,
        targets={"drawer", "kg"},
    )

    confirmed = await ledger.register(
        _intent("intent:2", source_event_id="turn-2"),
        targets={"drawer", "kg"},
    )

    assert confirmed.assertion_id == first.assertion_id
    assert confirmed.evidence_created is True
    assert confirmed.evidence_count == 2
    assert confirmed.pending_targets == []
    assert await ledger.evidence_count(first.assertion_id) == 2


@pytest.mark.asyncio
async def test_single_slot_rejects_parallel_active_values(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    first = _intent("intent:1", object_="常州").model_copy(
        update={"predicate": "lives_in", "raw_claim": "我住在常州"}
    )
    second = _intent(
        "intent:2", object_="苏州", source_event_id="turn-2"
    ).model_copy(update={"predicate": "lives_in", "raw_claim": "我住在苏州"})

    await ledger.register(first, targets={"kg"})

    with pytest.raises(CanonicalFactConflict, match="single predicate slot"):
        await ledger.register(second, targets={"kg"})

    active = await ledger.active_for_slot(MEMORY_SPACE_ID, "self", "lives_in")
    assert [fact.object for fact in active] == ["常州"]


@pytest.mark.asyncio
async def test_reactivation_stays_inactive_until_new_projections_are_visible(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    original = _intent("intent:1")
    await ledger.register(original, targets={"drawer", "kg"})
    await ledger.register_invalidation(_invalidation())
    await ledger.mark_invalidated(MEMORY_SPACE_ID, _invalidation().intent_id)
    reactivation = _intent(
        "intent:reactivate", source_event_id="turn-reactivate"
    ).model_copy(
        update={
            "operation_hint": "update",
            "raw_claim": "我又开始喜欢乌龙茶了",
            "occurred_at": "2026-07-01T00:00:00Z",
        }
    )

    pending = await ledger.register_reactivation(
        reactivation,
        targets={"drawer", "kg"},
    )

    assert pending.reactivation_pending is True
    assert pending.projection_id == f"{pending.assertion_id}:activation:2"
    assert (await ledger.get_fact(
        MEMORY_SPACE_ID, "self", "likes", "乌龙茶"
    )).state == "invalidated"
    with pytest.raises(RuntimeError, match="projections are still pending"):
        await ledger.mark_reactivated(MEMORY_SPACE_ID, reactivation.intent_id)

    await ledger.mark_projected(
        MEMORY_SPACE_ID,
        pending.assertion_id,
        targets={"drawer", "kg"},
    )
    await ledger.mark_reactivated(MEMORY_SPACE_ID, reactivation.intent_id)

    active = await ledger.get_fact(
        MEMORY_SPACE_ID, "self", "likes", "乌龙茶"
    )
    assert active is not None
    assert active.state == "active"
    assert active.projection_id.endswith(":activation:2")
    history = await ledger.history(
        MEMORY_SPACE_ID, "self", "likes", object_value="乌龙茶"
    )
    assert [event.transition for event in history[0].transitions] == [
        "invalidated",
        "reactivated",
    ]
    assert [item.intent_id for item in history[0].evidence] == [
        "intent:1",
        "intent:reactivate",
    ]
    stats = await ledger.stats()
    assert stats.reactivations_total == 1
    assert stats.reactivations_pending == 0


@pytest.mark.asyncio
async def test_intent_id_cannot_be_reused_for_a_different_fact(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    await ledger.register(
        _intent("intent:1", object_="乌龙茶"),
        targets={"drawer", "kg"},
    )

    with pytest.raises(CanonicalEvidenceConflict):
        await ledger.register(
            _intent("intent:1", object_="咖啡"),
            targets={"drawer", "kg"},
        )


@pytest.mark.asyncio
async def test_intent_id_cannot_change_evidence_for_the_same_fact(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    await ledger.register(
        _intent("intent:1", source_event_id="turn-1"),
        targets={"drawer", "kg"},
    )

    with pytest.raises(CanonicalEvidenceConflict):
        await ledger.register(
            _intent("intent:1", source_event_id="turn-2"),
            targets={"drawer", "kg"},
        )


@pytest.mark.asyncio
async def test_projection_state_is_independent_per_target(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    intent = _intent("intent:1")

    automatic = await ledger.register(intent, targets={"kg"})
    assert automatic.pending_targets == ["kg"]
    await ledger.mark_projected(
        MEMORY_SPACE_ID,
        automatic.assertion_id,
        targets={"kg"},
    )

    explicit = await ledger.register(intent, targets={"drawer", "kg"})

    assert explicit.pending_targets == ["drawer"]


@pytest.mark.asyncio
async def test_previous_combined_projection_state_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "canonical_facts.sqlite3"
    intent = _intent("intent:1")
    assertion_id = canonical_assertion_id(
        MEMORY_SPACE_ID,
        intent.subject,
        intent.predicate,
        intent.object,
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE canonical_assertions (
                assertion_id TEXT PRIMARY KEY,
                memory_space_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_value TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                projection_state TEXT NOT NULL DEFAULT 'pending',
                evidence_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_confirmed_at TEXT NOT NULL,
                UNIQUE(memory_space_id, subject, predicate, object_value)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO canonical_assertions (
                assertion_id, memory_space_id, subject, predicate,
                object_value, projection_state, created_at, updated_at,
                last_confirmed_at
            ) VALUES (?, ?, ?, ?, ?, 'projected', 'now', 'now', 'now')
            """,
            (
                assertion_id,
                MEMORY_SPACE_ID,
                intent.subject,
                intent.predicate,
                intent.object,
            ),
        )

    ledger = CanonicalFactLedger(path)
    registered = await ledger.register(intent, targets={"drawer", "kg"})

    assert registered.pending_targets == []


@pytest.mark.asyncio
async def test_stats_report_capacity_and_projection_state(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    automatic = await ledger.register(_intent("intent:1"), targets={"kg"})
    await ledger.mark_projected(
        MEMORY_SPACE_ID,
        automatic.assertion_id,
        targets={"kg"},
    )
    await ledger.register(
        _intent("intent:2", object_="咖啡", source_event_id="turn-2"),
        targets={"drawer", "kg"},
    )

    stats = await ledger.stats()

    assert stats.assertions_total == 2
    assert stats.evidence_total == 2
    assert stats.drawer_not_projected == 2
    assert stats.drawer_projected == 0
    assert stats.kg_not_projected == 1
    assert stats.kg_projected == 1
    assert stats.database_bytes > 0


@pytest.mark.asyncio
async def test_exact_invalidation_is_historical_and_replay_safe(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    original = _intent("intent:1")
    registered = await ledger.register(original, targets={"drawer", "kg"})
    await ledger.mark_projected(
        MEMORY_SPACE_ID,
        registered.assertion_id,
        targets={"drawer", "kg"},
    )

    first = await ledger.register_invalidation(_invalidation())
    assert (await ledger.stats()).invalidations_pending == 1
    await ledger.mark_invalidated(MEMORY_SPACE_ID, _invalidation().intent_id)
    replay = await ledger.register_invalidation(_invalidation())
    old_add_replay = await ledger.register(original, targets={"drawer", "kg"})
    stats = await ledger.stats()

    assert first.matched is True
    assert first.invalidation_created is True
    assert first.invalidation_count == 1
    assert replay.invalidation_created is False
    assert replay.state == "applied"
    assert replay.invalidation_count == 1
    assert old_add_replay.state == "invalidated"
    assert old_add_replay.pending_targets == []
    assert stats.assertions_active == 0
    assert stats.assertions_invalidated == 1
    assert stats.invalidations_total == 1
    assert stats.invalidations_pending == 0


@pytest.mark.asyncio
async def test_new_evidence_cannot_silently_reactivate_invalidated_fact(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    await ledger.register(_intent("intent:1"), targets={"kg"})
    await ledger.register_invalidation(_invalidation())
    await ledger.mark_invalidated(MEMORY_SPACE_ID, _invalidation().intent_id)

    with pytest.raises(CanonicalFactInactive):
        await ledger.register(
            _intent("intent:2", source_event_id="turn-later"),
            targets={"kg"},
        )


@pytest.mark.asyncio
async def test_invalidation_without_canonical_assertion_does_not_create_fact(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")

    result = await ledger.register_invalidation(_invalidation())
    stats = await ledger.stats()

    assert result.matched is False
    assert result.invalidation_created is False
    assert stats.assertions_total == 0
    assert stats.invalidations_total == 0


@pytest.mark.asyncio
async def test_invalidation_intent_conflict_is_rejected_while_pending(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    await ledger.register(_intent("intent:add-1"), targets={"kg"})
    await ledger.register(
        _intent("intent:add-2", object_="咖啡", source_event_id="turn-2"),
        targets={"kg"},
    )
    invalidation = _invalidation("intent:shared-invalidation")
    await ledger.register_invalidation(invalidation)

    with pytest.raises(CanonicalEvidenceConflict):
        await ledger.register_invalidation(
            invalidation.model_copy(
                update={
                    "object": "咖啡",
                    "raw_claim": "invalidate self likes 咖啡",
                }
            )
        )

    stats = await ledger.stats()
    assert stats.assertions_active == 2
    assert stats.invalidations_pending == 1
