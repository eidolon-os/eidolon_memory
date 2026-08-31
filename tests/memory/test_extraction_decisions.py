"""P1 extraction decisions are durable and reused across projection retries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_memory_contracts import (
    ConversationTurnPayload,
    MemoryActorContext,
    envelope_memory_payload,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.steward.llm import LiteLLMSteward
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
    extraction_input_hash,
)
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger

MEMORY_SPACE_ID = "r:alice:default"


def _turn(turn_id: str = "turn-1", *, user_text: str = "我喜欢绿茶") -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id=turn_id,
        context=MemoryActorContext(
            memory_realm_id=MEMORY_SPACE_ID,
            owner_id="alice",
            companion_id="default",
            device_id="device",
            session_id="session",
        ),
        timestamp="2026-07-16T10:00:00Z",
        user_text=user_text,
        assistant_text="我记住了",
    )


def _msg(turn: ConversationTurnPayload, *, deliveries: int = 1) -> SimpleNamespace:
    envelope = envelope_memory_payload(turn.model_dump(mode="json"), kind="conversation_turn")
    return SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode(),
        ack=AsyncMock(),
        nak=AsyncMock(),
        metadata=SimpleNamespace(num_delivered=deliveries),
    )


def _decision(turn: ConversationTurnPayload) -> StewardDecision:
    return StewardDecision(
        should_write=True,
        reason="explicit preference",
        fragments=[
            MemoryFragment(
                memory_space_id=MEMORY_SPACE_ID,
                memory_realm_id=MEMORY_SPACE_ID,
                wing="Wing_Life",
                room="preference_life",
                content="用户喜欢绿茶",
                memory_type="preference",
                importance=4,
                confidence=0.9,
                source_turn_id=turn.turn_id,
            )
        ],
    )


@pytest.mark.asyncio
async def test_decision_ledger_survives_reopen_and_rejects_changed_input(tmp_path: Path) -> None:
    turn = _turn()
    record = ExtractionDecisionRecord(
        memory_space_id=MEMORY_SPACE_ID,
        source_turn_id=turn.turn_id,
        extractor_version="rules:v1",
        input_hash=extraction_input_hash(turn),
        decision=_decision(turn),
    )
    path = tmp_path / "extraction_decisions.sqlite3"
    first = ExtractionDecisionLedger(path)
    stored = await first.put_if_absent(record)
    assert stored.decision.fragments[0].content == "用户喜欢绿茶"

    reopened = ExtractionDecisionLedger(path)
    loaded = await reopened.get(MEMORY_SPACE_ID, turn.turn_id, "rules:v1")
    assert loaded is not None
    assert loaded.input_hash == record.input_hash

    changed = record.model_copy(
        update={"input_hash": extraction_input_hash(_turn(user_text="改了"))}
    )
    with pytest.raises(ExtractionDecisionConflict):
        await reopened.put_if_absent(changed)


@pytest.mark.asyncio
async def test_privacy_tombstone_erases_decisions_and_blocks_every_extractor_version(
    tmp_path: Path,
) -> None:
    turn = _turn(user_text="不可恢复的紫色彗星")
    path = tmp_path / "extraction_decisions.sqlite3"
    store = ExtractionDecisionLedger(path)
    original = ExtractionDecisionRecord(
        memory_space_id=MEMORY_SPACE_ID,
        source_turn_id=turn.turn_id,
        extractor_version="rules:v1",
        input_hash=extraction_input_hash(turn),
        decision=_decision(turn),
    )
    await store.put_if_absent(original)

    assert await store.redact_source_events(MEMORY_SPACE_ID, [turn.turn_id]) == 1
    assert await store.redact_source_events(MEMORY_SPACE_ID, [turn.turn_id]) == 0

    for version in ("rules:v1", "llm:v99"):
        redacted = await store.get(MEMORY_SPACE_ID, turn.turn_id, version)
        assert redacted is not None
        assert redacted.redacted is True
        assert redacted.input_hash == ""
        assert redacted.intents == []
        assert redacted.decision.should_write is False

    replay = original.model_copy(update={"extractor_version": "llm:v99"})
    assert (await store.put_if_absent(replay)).redacted is True
    assert "不可恢复的紫色彗星".encode() not in path.read_bytes()


@pytest.mark.asyncio
async def test_privacy_redelivery_finishes_a_checkpoint_blocked_by_a_reader(
    tmp_path: Path,
) -> None:
    """A committed delete is not complete while its old bytes remain in WAL."""

    turn = _turn(user_text="只存在于待清理日志里的蓝色流星")
    path = tmp_path / "extraction_decisions.sqlite3"
    store = ExtractionDecisionLedger(path)
    await store.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id=MEMORY_SPACE_ID,
            source_turn_id=turn.turn_id,
            extractor_version="rules:v1",
            input_hash=extraction_input_hash(turn),
            decision=_decision(turn),
        )
    )

    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM extraction_decisions").fetchall()
    try:
        with pytest.raises(RuntimeError, match="checkpoint remained busy"):
            await store.redact_source_events(MEMORY_SPACE_ID, [turn.turn_id])
    finally:
        reader.close()

    # The row deletion and tombstone committed before the checkpoint reported
    # busy. Redelivery therefore changes no logical rows, but must still retry
    # the physical WAL truncation.
    assert await store.redact_source_events(MEMORY_SPACE_ID, [turn.turn_id]) == 0
    wal = path.with_name(f"{path.name}-wal")
    assert not wal.exists() or wal.stat().st_size == 0


@pytest.mark.asyncio
async def test_projection_retry_reuses_persisted_decision_without_rerunning_steward(
    tmp_path: Path,
) -> None:
    turn = _turn()
    steward = SimpleNamespace(
        extraction_version="test:v1",
        decide=AsyncMock(return_value=_decision(turn)),
    )

    class FailFirstProjection(FakeMemoryBackend):
        def __init__(self) -> None:
            super().__init__()
            self.failures_left = 1

        async def ingest_fragment(self, fragment: MemoryFragment) -> None:
            if self.failures_left:
                self.failures_left -= 1
                raise RuntimeError("transient projection failure")
            await super().ingest_fragment(fragment)

    backend = LockedBackend(FailFirstProjection())
    store = ExtractionDecisionLedger(tmp_path / "extraction_decisions.sqlite3")
    canonical_path = tmp_path / "canonical.sqlite3"
    settings = load_memory_settings()

    first = _msg(turn, deliveries=1)
    await process_turn_message(
        first,
        steward=steward,
        backend=backend,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        decision_store=store,
        canonical_facts=CanonicalFactLedger(canonical_path),
    )
    first.nak.assert_awaited_once()

    second = _msg(turn, deliveries=2)
    await process_turn_message(
        second,
        steward=steward,
        backend=backend,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        decision_store=ExtractionDecisionLedger(store.path),
        canonical_facts=CanonicalFactLedger(canonical_path),
    )

    steward.decide.assert_awaited_once()
    second.ack.assert_awaited_once()
    assert len(backend.inner.docs) == 1
    stored = await store.get(MEMORY_SPACE_ID, turn.turn_id, "test:v1")
    assert stored is not None
    assert len(stored.intents) == 1
    assert stored.intents[0].intent_type == "preference"
    assert stored.intents[0].source_event_id == turn.turn_id


@pytest.mark.asyncio
async def test_same_turn_redelivered_100_times_has_one_extraction(tmp_path: Path) -> None:
    turn = _turn(turn_id="turn-100")
    steward = SimpleNamespace(
        extraction_version="test:v1",
        decide=AsyncMock(return_value=_decision(turn)),
    )
    backend = LockedBackend(FakeMemoryBackend())
    settings = load_memory_settings()
    path = tmp_path / "extraction_decisions.sqlite3"
    canonical_path = tmp_path / "canonical.sqlite3"

    for delivery in range(1, 101):
        msg = _msg(turn, deliveries=delivery)
        await process_turn_message(
            msg,
            steward=steward,
            backend=backend,
            settings=settings,
            max_deliveries=101,
            expected_memory_space_id=MEMORY_SPACE_ID,
            decision_store=ExtractionDecisionLedger(path),
            canonical_facts=CanonicalFactLedger(canonical_path),
        )
        msg.ack.assert_awaited_once()

    steward.decide.assert_awaited_once()
    assert len(backend.inner.docs) == 1


@pytest.mark.asyncio
async def test_same_turn_id_with_changed_input_fails_closed(tmp_path: Path) -> None:
    original = _turn(turn_id="turn-conflict", user_text="我喜欢绿茶")
    store = ExtractionDecisionLedger(tmp_path / "extraction_decisions.sqlite3")
    await store.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id=MEMORY_SPACE_ID,
            source_turn_id=original.turn_id,
            extractor_version="test:v1",
            input_hash=extraction_input_hash(original),
            decision=_decision(original),
        )
    )
    steward = SimpleNamespace(
        extraction_version="test:v1",
        decide=AsyncMock(return_value=_decision(original)),
    )
    changed = _turn(turn_id="turn-conflict", user_text="我讨厌绿茶")
    msg = _msg(changed, deliveries=1)

    await process_turn_message(
        msg,
        steward=steward,
        backend=LockedBackend(FakeMemoryBackend()),
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        decision_store=store,
    )

    msg.nak.assert_awaited_once()
    steward.decide.assert_not_awaited()


def test_llm_extraction_version_changes_with_decision_policy() -> None:
    settings = load_memory_settings().model_copy(deep=True)
    settings.llm.model = "openai/test-model"
    settings.llm.temperature = 0.0
    stable = LiteLLMSteward(settings).extraction_version
    assert stable == LiteLLMSteward(settings.model_copy(deep=True)).extraction_version

    changed = settings.model_copy(deep=True)
    changed.llm.temperature = 0.2
    assert LiteLLMSteward(changed).extraction_version != stable
