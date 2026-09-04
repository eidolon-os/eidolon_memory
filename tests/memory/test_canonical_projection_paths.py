"""Canonical fact identity is shared by automatic and explicit write paths."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eidolon_memory_contracts import (
    MemoryIntent,
    MemoryIntentCommand,
    companion_audience,
    envelope_memory_payload,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.explicit_intents import (
    MemoryIntentRejected,
    _projection_room_token,
    apply_explicit_intent,
)
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.canonical_fact import canonical_assertion_id
from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger

MEMORY_SPACE_ID = "r:alice:default"
AUDIENCE = companion_audience("companion-default")


class _StatefulKG:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], SimpleNamespace] = {}
        self.add_triple = AsyncMock(side_effect=self._add_triple)
        self.query_entity = AsyncMock(side_effect=self._query_entity)
        self.invalidate = AsyncMock(side_effect=self._invalidate)
        self.record_entity_mention = AsyncMock()

    async def _add_triple(self, **kwargs) -> str:
        key = (kwargs["subject"], kwargs["predicate"], kwargs["object"])
        self.rows[key] = SimpleNamespace(
            subject=key[0],
            predicate=key[1],
            object=key[2],
        )
        return "triple-1"

    async def _query_entity(self, entity_id: str, **_kwargs) -> list[SimpleNamespace]:
        return [row for row in self.rows.values() if row.subject == entity_id]

    async def _invalidate(self, **kwargs) -> int:
        key = (kwargs["subject"], kwargs["predicate"], kwargs["object"])
        return int(self.rows.pop(key, None) is not None)


class _FailOnceMarkStore:
    def __init__(self, inner: CanonicalFactLedger, target: str) -> None:
        self.inner = inner
        self.target = target
        self.failed = False

    async def register(self, intent, *, targets):
        return await self.inner.register(intent, targets=targets)

    async def get_fact(self, memory_space_id, audience, subject, predicate, object_value):
        return await self.inner.get_fact(
            memory_space_id, audience, subject, predicate, object_value
        )

    async def register_reactivation(self, intent, *, targets):
        return await self.inner.register_reactivation(intent, targets=targets)

    async def mark_reactivated(self, memory_space_id, intent_id, *, targets=None):
        await self.inner.mark_reactivated(memory_space_id, intent_id, targets=targets)

    async def mark_projection_pending(
        self,
        memory_space_id,
        assertion_id,
        *,
        targets,
    ) -> None:
        await self.inner.mark_projection_pending(
            memory_space_id,
            assertion_id,
            targets=targets,
        )

    async def mark_projected(
        self,
        memory_space_id,
        assertion_id,
        *,
        targets,
    ) -> None:
        if self.target in targets and not self.failed:
            self.failed = True
            raise RuntimeError(f"fail marking {self.target}")
        await self.inner.mark_projected(
            memory_space_id,
            assertion_id,
            targets=targets,
        )


def _turn_message(
    turn_id: str, *, companion_id: str | None = "companion-default"
) -> SimpleNamespace:
    payload = {
        "turn_id": turn_id,
        "context": {
            "owner_id": "alice",
            "companion_id": companion_id,
            "memory_realm_id": MEMORY_SPACE_ID,
            "device_id": "device",
            "session_id": "session",
        },
        "timestamp": "2026-06-01T00:00:00Z",
        "user_text": "我喜欢乌龙茶",
        "assistant_text": "记住了",
    }
    envelope = envelope_memory_payload(payload, kind="conversation_turn")
    return SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode(),
        ack=AsyncMock(),
        nak=AsyncMock(),
        metadata=SimpleNamespace(num_delivered=1),
    )


def _steward() -> MagicMock:
    decision = StewardDecision(
        should_write=True,
        reason="explicit preference",
        triples=[
            KgTripleAction(
                subject="self",
                predicate="likes",
                object="oolong",
                confidence=0.95,
            )
        ],
    )
    steward = MagicMock()
    steward.extraction_version = "test-extractor"
    steward.decide = AsyncMock(return_value=decision)
    return steward


def _explicit_command(
    *,
    request_id: str = "explicit-1",
    intent_id: str = "intent:explicit-1",
    predicate: str = "likes",
    object_: str = "oolong",
    operation_hint: str = "confirm",
    raw_claim: str = "我喜欢乌龙茶",
) -> MemoryIntentCommand:
    return MemoryIntentCommand(
        request_id=request_id,
        memory_space_id=MEMORY_SPACE_ID,
        issued_at="2026-06-01T00:01:00Z",
        issuer="agent",
        intent=MemoryIntent(
            intent_id=intent_id,
            memory_space_id=MEMORY_SPACE_ID,
            source_event_id="turn-explicit",
            authority="explicit_user",
            intent_type="preference",
            raw_claim=raw_claim,
            operation_hint=operation_hint,
            subject="self",
            predicate=predicate,
            object=object_,
            confidence=0.99,
            attributes={"source_instance_id": "companion-default"},
        ),
    )


def _exact_correction_command() -> MemoryIntentCommand:
    return MemoryIntentCommand(
        request_id="correction-1",
        memory_space_id=MEMORY_SPACE_ID,
        issued_at="2026-06-02T00:00:00Z",
        issuer="agent",
        intent=MemoryIntent(
            intent_id="intent:correction-1",
            memory_space_id=MEMORY_SPACE_ID,
            source_event_id="turn-correction",
            authority="explicit_user",
            intent_type="correction",
            raw_claim="我不再喜欢乌龙茶",
            operation_hint="invalidate",
            subject="self",
            predicate="likes",
            object="oolong",
            confidence=1.0,
            attributes={"source_instance_id": "companion-default"},
        ),
    )


async def _apply_automatic(
    turn_id: str,
    *,
    backend: LockedBackend,
    kg: _StatefulKG,
    ledger: CanonicalFactLedger,
) -> None:
    msg = _turn_message(turn_id)
    await process_turn_message(
        msg,
        steward=_steward(),
        backend=backend,
        kg=kg,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=ledger,
    )
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


async def _apply_decision(
    turn_id: str,
    decision: StewardDecision,
    *,
    backend: LockedBackend,
    kg: _StatefulKG,
    ledger: CanonicalFactLedger,
) -> None:
    steward = MagicMock()
    steward.extraction_version = "test-extractor"
    steward.decide = AsyncMock(return_value=decision)
    msg = _turn_message(turn_id)
    await process_turn_message(
        msg,
        steward=steward,
        backend=backend,
        kg=kg,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=ledger,
    )
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


@pytest.mark.asyncio
async def test_automatic_then_explicit_adds_evidence_and_only_projects_drawer(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    await _apply_automatic("turn-auto", backend=backend, kg=kg, ledger=ledger)
    await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(),
        canonical_facts=ledger,
    )

    assertion_id = canonical_assertion_id(
        MEMORY_SPACE_ID,
        AUDIENCE,
        "self",
        "likes",
        "oolong",
    )
    assert kg.add_triple.await_count == 1
    assert len(backend.inner.docs) == 1
    assert await ledger.evidence_count(assertion_id) == 2


@pytest.mark.asyncio
async def test_explicit_projection_uses_predicate_ontology_not_claim_words(tmp_path) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    kg = _StatefulKG()
    command = _explicit_command(raw_claim="这句话刻意不包含任何偏好触发词")

    await apply_explicit_intent(backend, kg, command, canonical_facts=ledger)

    record = next(iter(backend.inner.docs.values()))
    assert record.metadata["wing"] == "Wing_Life"
    assert record.metadata["memory_type"] == "preference"
    assert record.metadata["source_event_id"] == "turn-explicit"


@pytest.mark.asyncio
async def test_verbatim_explicit_projection_requires_typed_destination(tmp_path) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    command = _explicit_command().model_copy(
        update={
            "intent": _explicit_command().intent.model_copy(
                update={"subject": None, "predicate": None, "object": None}
            )
        }
    )

    with pytest.raises(
        MemoryIntentRejected,
        match="requires explicit wing and memory_type",
    ):
        await apply_explicit_intent(
            backend,
            _StatefulKG(),
            command,
            canonical_facts=ledger,
        )


@pytest.mark.asyncio
async def test_explicit_projection_rejects_destination_conflicting_with_predicate(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    base = _explicit_command()
    command = base.model_copy(
        update={
            "intent": base.intent.model_copy(
                update={"attributes": {**base.intent.attributes, "wing": "Wing_Work"}}
            )
        }
    )

    with pytest.raises(MemoryIntentRejected, match="conflicts with predicate"):
        await apply_explicit_intent(
            backend,
            _StatefulKG(),
            command,
            canonical_facts=ledger,
        )


@pytest.mark.asyncio
async def test_explicit_then_automatic_reuses_both_existing_projections(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(),
        canonical_facts=ledger,
    )
    await _apply_automatic("turn-auto", backend=backend, kg=kg, ledger=ledger)

    assertion_id = canonical_assertion_id(
        MEMORY_SPACE_ID,
        AUDIENCE,
        "self",
        "likes",
        "oolong",
    )
    assert kg.add_triple.await_count == 1
    assert len(backend.inner.docs) == 1
    assert await ledger.evidence_count(assertion_id) == 2


@pytest.mark.asyncio
async def test_repeated_automatic_fact_keeps_one_projection_and_two_evidence(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    await _apply_automatic("turn-auto-1", backend=backend, kg=kg, ledger=ledger)
    await _apply_automatic("turn-auto-2", backend=backend, kg=kg, ledger=ledger)

    assertion_id = canonical_assertion_id(
        MEMORY_SPACE_ID,
        AUDIENCE,
        "self",
        "likes",
        "oolong",
    )
    assert kg.add_triple.await_count == 1
    assert len(backend.inner.docs) == 1
    assert await ledger.evidence_count(assertion_id) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_target", ["drawer", "kg"])
async def test_explicit_retry_repairs_mark_failure_without_rewriting_projection(
    tmp_path,
    failed_target,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    store = _FailOnceMarkStore(ledger, failed_target)

    with pytest.raises(RuntimeError, match=f"fail marking {failed_target}"):
        await apply_explicit_intent(
            backend,
            kg,
            _explicit_command(),
            canonical_facts=store,
        )
    await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(),
        canonical_facts=store,
    )

    assert len(backend.inner.ingests) == 1
    assert kg.add_triple.await_count == 1


@pytest.mark.asyncio
async def test_automatic_new_evidence_repairs_mark_failure_without_kg_rewrite(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    store = _FailOnceMarkStore(ledger, "kg")

    first = _turn_message("turn-auto-1")
    await process_turn_message(
        first,
        steward=_steward(),
        backend=backend,
        kg=kg,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=store,
    )
    first.nak.assert_awaited_once()
    first.ack.assert_not_awaited()
    await _apply_automatic("turn-auto-1", backend=backend, kg=kg, ledger=store)
    await _apply_automatic("turn-auto-2", backend=backend, kg=kg, ledger=store)

    assert kg.add_triple.await_count == 1


@pytest.mark.asyncio
async def test_exact_change_archives_canonical_drawer_and_keeps_fact_history(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(),
        canonical_facts=ledger,
    )
    decision = StewardDecision(
        should_write=True,
        reason="changed preference",
        triples=[
            KgTripleAction(
                subject="self",
                predicate="likes",
                object="tea",
                confidence=0.95,
            )
        ],
        invalidations=[
            KgInvalidationAction(
                subject="self",
                predicate="likes",
                object="oolong",
                reason="user changed preference",
            )
        ],
    )

    await _apply_decision(
        "turn-change",
        decision,
        backend=backend,
        kg=kg,
        ledger=ledger,
    )

    old_drawer = await backend.get_by_source_turn_id(
        MEMORY_SPACE_ID,
        "canonical:" + canonical_assertion_id(MEMORY_SPACE_ID, AUDIENCE, "self", "likes", "oolong"),
    )
    assert old_drawer is not None
    assert old_drawer.metadata["privacy"] == "do_not_recall"
    assert ("self", "likes", "oolong") not in kg.rows
    assert ("self", "likes", "tea") in kg.rows
    stats = await ledger.stats()
    assert stats.assertions_active == 1
    assert stats.assertions_invalidated == 1
    assert stats.invalidations_total == 1
    assert stats.drawer_projected == 1
    assert stats.kg_projected == 1


@pytest.mark.asyncio
async def test_automatic_fact_can_become_current_again_after_correction(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    await _apply_automatic("turn-first", backend=backend, kg=kg, ledger=ledger)
    await _apply_decision(
        "turn-stop",
        StewardDecision(
            should_write=False,
            reason="preference ended",
            invalidations=[
                KgInvalidationAction(
                    subject="self",
                    predicate="likes",
                    object="oolong",
                    reason="user no longer likes it",
                )
            ],
        ),
        backend=backend,
        kg=kg,
        ledger=ledger,
    )
    await _apply_automatic("turn-again", backend=backend, kg=kg, ledger=ledger)

    assert ("self", "likes", "oolong") in kg.rows
    assert kg.add_triple.await_count == 2
    fact = await ledger.get_fact(MEMORY_SPACE_ID, AUDIENCE, "self", "likes", "oolong")
    assert fact is not None
    assert fact.state == "active"
    assert fact.projection_id.endswith(":activation:2")
    stats = await ledger.stats()
    assert stats.reactivations_total == 1
    assert stats.reactivations_pending == 0


@pytest.mark.asyncio
async def test_explicit_single_slot_update_supersedes_old_fact_via_existing_ports(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(
            predicate="lives_in",
            object_="常州",
            raw_claim="我住在常州",
        ),
        canonical_facts=ledger,
    )
    result = await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(
            request_id="explicit-2",
            intent_id="intent:explicit-2",
            predicate="lives_in",
            object_="苏州",
            operation_hint="update",
            raw_claim="我现在住在苏州",
        ),
        canonical_facts=ledger,
    )

    assert result.startswith("memoryintent:fact:")
    assert ("self", "lives_in", "常州") not in kg.rows
    assert ("self", "lives_in", "苏州") in kg.rows
    old_id = canonical_assertion_id(MEMORY_SPACE_ID, AUDIENCE, "self", "lives_in", "常州")
    old_drawer = await backend.get_by_source_turn_id(MEMORY_SPACE_ID, f"canonical:{old_id}")
    assert old_drawer is not None
    assert old_drawer.metadata["privacy"] == "do_not_recall"
    active = await ledger.active_for_slot(MEMORY_SPACE_ID, AUDIENCE, "self", "lives_in")
    assert [fact.object for fact in active] == ["苏州"]
    stats = await ledger.stats()
    assert stats.assertions_active == 1
    assert stats.assertions_superseded == 1
    assert stats.assertions_invalidated == 0
    assert stats.supersessions_total == 1
    assert stats.invalidations_total == 0

    replay = await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(
            predicate="lives_in",
            object_="常州",
            raw_claim="我住在常州",
        ),
        canonical_facts=ledger,
    )
    assert replay.startswith("superseded:")
    assert ("self", "lives_in", "常州") not in kg.rows
    assert ("self", "lives_in", "苏州") in kg.rows


@pytest.mark.asyncio
async def test_explicit_update_does_not_guess_replacement_for_multi_value_fact(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(),
        canonical_facts=ledger,
    )

    with pytest.raises(ValueError, match="requires exact correction"):
        await apply_explicit_intent(
            backend,
            kg,
            _explicit_command(
                request_id="explicit-2",
                intent_id="intent:explicit-2",
                object_="coffee",
                operation_hint="update",
                raw_claim="我现在更喜欢咖啡",
            ),
            canonical_facts=ledger,
        )

    assert ("self", "likes", "oolong") in kg.rows
    assert ("self", "likes", "coffee") not in kg.rows


@pytest.mark.asyncio
async def test_explicit_exact_update_reactivates_without_stale_replay_damage(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    original = _explicit_command()
    correction = _exact_correction_command()

    await apply_explicit_intent(backend, kg, original, canonical_facts=ledger)
    await apply_explicit_intent(backend, kg, correction, canonical_facts=ledger)
    reactivated = await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(
            request_id="reactivate-1",
            intent_id="intent:reactivate-1",
            operation_hint="update",
            raw_claim="我又开始喜欢乌龙茶了",
        ),
        canonical_facts=ledger,
    )

    assert reactivated.startswith("reactivated:")
    assert ("self", "likes", "oolong") in kg.rows
    records = await backend.get_all(MEMORY_SPACE_ID)
    matching = [row for row in records if "乌龙茶" in str(row.value)]
    assert len(matching) == 2
    assert sum(row.metadata.get("privacy") == "do_not_recall" for row in matching) == 1
    assert sum(row.metadata.get("privacy") == "normal" for row in matching) == 1

    stale_replay = await apply_explicit_intent(backend, kg, correction, canonical_facts=ledger)
    assert stale_replay.startswith("invalidated:")
    assert ("self", "likes", "oolong") in kg.rows
    history = await ledger.history(MEMORY_SPACE_ID, "self", "likes", object_value="oolong")
    assert history[0].fact.state == "active"
    assert [event.transition for event in history[0].transitions] == [
        "invalidated",
        "reactivated",
    ]


def test_reactivation_projection_rooms_do_not_collide_between_facts() -> None:
    first = _projection_room_token("fact:first:activation:2")
    second = _projection_room_token("fact:second:activation:2")

    assert first != second
    assert len(first) == 16
    assert _projection_room_token("fact:0123456789abcdef") == "0123456789abcdef"


@pytest.mark.asyncio
async def test_same_natural_fact_is_isolated_per_companion(tmp_path) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    for turn_id, companion_id in (("turn-a", "companion-default"), ("turn-b", "other")):
        msg = _turn_message(turn_id, companion_id=companion_id)
        await process_turn_message(
            msg,
            steward=_steward(),
            backend=backend,
            kg=kg,
            settings=load_memory_settings(),
            max_deliveries=3,
            expected_memory_space_id=MEMORY_SPACE_ID,
            canonical_facts=ledger,
        )
        msg.ack.assert_awaited_once()

    docs = await backend.get_all(MEMORY_SPACE_ID)
    assert {row.metadata["audience"] for row in docs} == {
        AUDIENCE,
        companion_audience("other"),
    }
    assert len({row.metadata["assertion_id"] for row in docs}) == 2
    assert {row.metadata["source_event_id"] for row in docs} == {"turn-a", "turn-b"}
    calls = kg.add_triple.await_args_list
    assert {call.kwargs["audience"] for call in calls} == {
        AUDIENCE,
        companion_audience("other"),
    }
    assert {call.kwargs["assertion_id"] for call in calls} == {
        row.metadata["assertion_id"] for row in docs
    }
    assert (await ledger.stats()).assertions_total == 2


@pytest.mark.asyncio
async def test_missing_interaction_identity_is_dlqd_without_owner_fallback(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    steward = _steward()
    dlq = SimpleNamespace(add=AsyncMock())
    msg = _turn_message("turn-no-identity", companion_id=None)

    await process_turn_message(
        msg,
        steward=steward,
        backend=backend,
        kg=kg,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=ledger,
        dlq_writer=dlq,
    )

    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    steward.decide.assert_not_awaited()
    dlq.add.assert_awaited_once()
    assert await backend.get_all(MEMORY_SPACE_ID) == []
    assert kg.add_triple.await_count == 0


@pytest.mark.asyncio
async def test_automatic_exact_invalidation_naks_then_repairs_pending_state(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(),
        canonical_facts=ledger,
    )
    decision = StewardDecision(
        should_write=True,
        reason="changed preference",
        invalidations=[
            KgInvalidationAction(
                subject="self",
                predicate="likes",
                object="oolong",
            )
        ],
    )
    attempts = 0

    async def _fail_once(**kwargs) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary invalidation failure")
        return await kg._invalidate(**kwargs)

    kg.invalidate.side_effect = _fail_once
    steward = MagicMock()
    steward.extraction_version = "test-extractor"
    steward.decide = AsyncMock(return_value=decision)

    first = _turn_message("turn-auto-correction")
    await process_turn_message(
        first,
        steward=steward,
        backend=backend,
        kg=kg,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=ledger,
        decision_store=decisions,
    )
    assert first.nak.await_count == 1
    first.ack.assert_not_awaited()
    pending = await ledger.stats()
    assert pending.assertions_active == 1
    assert pending.invalidations_pending == 1

    second = _turn_message("turn-auto-correction")
    second.metadata.num_delivered = 2
    await process_turn_message(
        second,
        steward=steward,
        backend=backend,
        kg=kg,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=ledger,
        decision_store=decisions,
    )
    second.ack.assert_awaited_once()
    second.nak.assert_not_awaited()
    completed = await ledger.stats()
    assert completed.assertions_active == 0
    assert completed.assertions_invalidated == 1
    assert completed.invalidations_pending == 0
    assert completed.invalidations_total == 1


@pytest.mark.asyncio
async def test_automatic_exact_invalidation_enters_dlq_at_delivery_limit(
    tmp_path,
) -> None:
    backend = LockedBackend(FakeMemoryBackend())
    kg = _StatefulKG()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    await apply_explicit_intent(
        backend,
        kg,
        _explicit_command(),
        canonical_facts=ledger,
    )
    kg.invalidate.side_effect = RuntimeError("persistent invalidation failure")
    decision = StewardDecision(
        should_write=True,
        reason="changed preference",
        invalidations=[
            KgInvalidationAction(
                subject="self",
                predicate="likes",
                object="oolong",
            )
        ],
    )
    steward = MagicMock()
    steward.extraction_version = "test-extractor"
    steward.decide = AsyncMock(return_value=decision)
    dlq = SimpleNamespace(add=AsyncMock(return_value=SimpleNamespace()))
    msg = _turn_message("turn-auto-correction-dlq")
    msg.metadata.num_delivered = 3

    await process_turn_message(
        msg,
        steward=steward,
        backend=backend,
        kg=kg,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=ledger,
        dlq_writer=dlq,
        decision_store=decisions,
    )

    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    dlq.add.assert_awaited_once()
    assert "persistent invalidation failure" in dlq.add.await_args.kwargs["error"]
    stats = await ledger.stats()
    assert stats.assertions_active == 1
    assert stats.invalidations_pending == 1
