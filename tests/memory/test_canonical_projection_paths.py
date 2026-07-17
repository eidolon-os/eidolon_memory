"""Canonical fact identity is shared by automatic and explicit write paths."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eidolon_sdk.memory import (
    MemoryIntent,
    MemoryIntentCommand,
    envelope_memory_payload,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.explicit_intents import apply_explicit_intent
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.canonical_fact import canonical_assertion_id
from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger

MEMORY_SPACE_ID = "r:alice:default"


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


def _turn_message(turn_id: str) -> SimpleNamespace:
    payload = {
        "turn_id": turn_id,
        "context": {
            "owner_id": "alice",
            "companion_id": "companion-default",
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


def _explicit_command() -> MemoryIntentCommand:
    return MemoryIntentCommand(
        request_id="explicit-1",
        memory_space_id=MEMORY_SPACE_ID,
        issued_at="2026-06-01T00:01:00Z",
        issuer="agent",
        intent=MemoryIntent(
            intent_id="intent:explicit-1",
            memory_space_id=MEMORY_SPACE_ID,
            source_event_id="turn-explicit",
            authority="explicit_user",
            intent_type="preference",
            raw_claim="我喜欢乌龙茶",
            operation_hint="confirm",
            subject="self",
            predicate="likes",
            object="oolong",
            confidence=0.99,
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
        "self",
        "likes",
        "oolong",
    )
    assert kg.add_triple.await_count == 1
    assert len(backend.inner.docs) == 1
    assert await ledger.evidence_count(assertion_id) == 2


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
        "self",
        "likes",
        "oolong",
    )
    assert kg.add_triple.await_count == 1
    assert len(backend.inner.docs) == 0
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
        "canonical:"
        + canonical_assertion_id(MEMORY_SPACE_ID, "self", "likes", "oolong"),
    )
    assert old_drawer is not None
    assert old_drawer.metadata["privacy"] == "do_not_recall"
    assert ("self", "likes", "oolong") not in kg.rows
    assert ("self", "likes", "tea") in kg.rows
    stats = await ledger.stats()
    assert stats.assertions_active == 1
    assert stats.assertions_invalidated == 1
    assert stats.invalidations_total == 1
    assert stats.drawer_projected == 0
    assert stats.kg_projected == 1
