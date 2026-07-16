"""Canonical exact facts retain confirmation evidence across projection retries."""

from __future__ import annotations

from pathlib import Path

import pytest
from eidolon_sdk.memory import MemoryIntent

from eidolon.memory.domain.canonical_fact import CanonicalEvidenceConflict
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


@pytest.mark.asyncio
async def test_projection_is_required_until_marked_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    intent = _intent("intent:1")

    first = await ledger.register(intent)
    retry = await ledger.register(intent)

    assert first.assertion_id == retry.assertion_id
    assert first.evidence_created is True
    assert retry.evidence_created is False
    assert retry.evidence_count == 1
    assert retry.projection_required is True

    await ledger.mark_projected(MEMORY_SPACE_ID, first.assertion_id)
    reopened = CanonicalFactLedger(ledger.path)
    after_projection = await reopened.register(intent)
    assert after_projection.projection_required is False
    assert after_projection.evidence_count == 1


@pytest.mark.asyncio
async def test_new_confirmation_keeps_one_fact_and_adds_provenance(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    first = await ledger.register(_intent("intent:1"))
    await ledger.mark_projected(MEMORY_SPACE_ID, first.assertion_id)

    confirmed = await ledger.register(
        _intent("intent:2", source_event_id="turn-2")
    )

    assert confirmed.assertion_id == first.assertion_id
    assert confirmed.evidence_created is True
    assert confirmed.evidence_count == 2
    assert confirmed.projection_required is False
    assert await ledger.evidence_count(first.assertion_id) == 2


@pytest.mark.asyncio
async def test_intent_id_cannot_be_reused_for_a_different_fact(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    await ledger.register(_intent("intent:1", object_="乌龙茶"))

    with pytest.raises(CanonicalEvidenceConflict):
        await ledger.register(_intent("intent:1", object_="咖啡"))


@pytest.mark.asyncio
async def test_intent_id_cannot_change_evidence_for_the_same_fact(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    await ledger.register(_intent("intent:1", source_event_id="turn-1"))

    with pytest.raises(CanonicalEvidenceConflict):
        await ledger.register(_intent("intent:1", source_event_id="turn-2"))
