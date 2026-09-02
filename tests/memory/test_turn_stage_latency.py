"""Where a turn's wall time went.

Absorption waits on an LLM and then on three storages, and until now only the
LLM half was measured. An end-to-end latency outlier could therefore be
observed but not attributed: a 59-second materialisation looked the same
whether the steward thought for 55 seconds or the message sat unconsumed.

These tests pin the attribution itself — which stages are recorded, which are
deliberately absent, and that a failure is still measured.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eidolon_memory_contracts import envelope_memory_payload

MEMORY_SPACE_ID = "r:alice:default"


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
def canonical(tmp_path: Path):
    from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger

    return CanonicalFactLedger(tmp_path / "canonical.sqlite3")


def _turn_payload(**kwargs) -> dict:
    return {
        "turn_id": kwargs.get("turn_id") or uuid.uuid4().hex,
        "context": {
            "owner_id": "alice",
            "companion_id": "test",
            "memory_realm_id": MEMORY_SPACE_ID,
            "device_id": "device",
            "session_id": "s1",
        },
        "timestamp": "2026-05-19T10:00:00Z",
        "user_text": kwargs.get("user_text", "hello"),
        "assistant_text": "hi",
    }


def _stub_msg(payload: dict) -> SimpleNamespace:
    ack_calls: list[str] = []
    nak_calls: list[str] = []

    async def _ack():
        ack_calls.append("ack")

    async def _nak():
        nak_calls.append("nak")

    envelope = envelope_memory_payload(payload, kind="conversation_turn")
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


def _fragment_decision():
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.steward import StewardDecision

    fragment = MemoryFragment(
        memory_id="f1",
        memory_space_id=MEMORY_SPACE_ID,
        source_device_id="device",
        source_instance_id="test",
        wing="Wing_Profile",
        room="profile_core",
        content="user likes tea",
        memory_type="preference",
        importance=4,
        confidence=0.95,
        source_turn_id="t1",
        session_id="s1",
    )
    return StewardDecision(should_write=True, reason="ok", fragments=[fragment])


def _stage_counts() -> dict[str, float]:
    """Observation counts per stage, so a test can measure its own delta.

    Counts rather than sums: a duration assertion would be a clock test, and
    the question here is which stages ran at all.
    """
    from prometheus_client import REGISTRY

    counts: dict[str, float] = {}
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == "eidolon_memory_turn_stage_seconds_count":
                counts[sample.labels["stage"]] = sample.value
    return counts


def _delta(before: dict[str, float], after: dict[str, float]) -> set[str]:
    return {stage for stage, value in after.items() if value > before.get(stage, 0.0)}


async def test_a_fragment_turn_reports_the_stages_it_actually_ran(
    settings, backend, canonical
) -> None:
    """Absent stages matter as much as present ones.

    A stage entered unconditionally would push a zero into the histogram on
    every quiet turn and drag the tail down, which is the failure mode that
    makes a percentile stop meaning anything.
    """
    from eidolon.memory.application.turn_processor import process_turn_message

    before = _stage_counts()
    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg,
        steward=_make_steward(_fragment_decision()),
        backend=backend,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )

    assert msg.ack_calls == ["ack"]
    # No ``verbatim``: the layer ships off, because the drawers it writes
    # cannot be forgotten. See test_the_shipped_default_keeps_the_layer_off.
    assert _delta(before, _stage_counts()) == {"steward", "fragments", "total"}


async def test_a_graph_turn_separates_graph_time_from_steward_time(
    settings, backend, canonical
) -> None:
    """The two slow halves of absorption must not share one number."""
    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.domain.kg import KgTripleAction

    kg = MagicMock()
    kg.lock = backend.lock
    kg.add_triple = AsyncMock(return_value=None)
    kg.invalidate = AsyncMock(return_value=0)
    kg.query_entity = AsyncMock(return_value=[])

    decision = _fragment_decision()
    decision = decision.model_copy(
        update={
            "triples": [
                KgTripleAction(subject="self", predicate="likes", object="tea", confidence=0.9)
            ]
        }
    )

    before = _stage_counts()
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

    ran = _delta(before, _stage_counts())
    assert "kg" in ran
    assert "steward" in ran
    # The triple's own drawer is projected inside the graph stage, so the
    # fragment-only stage must not also claim this turn.
    assert "fragments" not in ran


async def test_a_steward_that_raises_is_still_measured(settings, backend, canonical) -> None:
    """The slow path is the one worth timing, and failures are often the slow path.

    The previous hand-written timing recorded both paths. Moving it into a
    context manager must not quietly drop the failure one.
    """
    from eidolon.memory.application.turn_processor import process_turn_message

    steward = MagicMock()
    steward.extraction_version = "test:v1"
    steward.decide = AsyncMock(side_effect=RuntimeError("model unreachable"))

    before = _stage_counts()
    msg = _stub_msg(_turn_payload())
    await process_turn_message(
        msg,
        steward=steward,
        backend=backend,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
        canonical_facts=canonical,
    )

    ran = _delta(before, _stage_counts())
    assert "steward" in ran
    # A turn discarded before any projection ran did not absorb anything, and
    # counting it as a fast ``total`` would make the discard path look like
    # healthy throughput.
    assert "total" not in ran


async def test_one_turn_names_its_own_slow_stage_in_the_log(
    settings, backend, canonical, caplog
) -> None:
    """A percentile cannot name a turn, and an outlier is always about one turn."""
    from eidolon.memory.application.turn_processor import process_turn_message

    payload = _turn_payload()
    msg = _stub_msg(payload)
    with caplog.at_level(logging.INFO, logger="eidolon.memory.application.turn_processor"):
        await process_turn_message(
            msg,
            steward=_make_steward(_fragment_decision()),
            backend=backend,
            settings=settings,
            max_deliveries=3,
            expected_memory_space_id=MEMORY_SPACE_ID,
            canonical_facts=canonical,
        )

    line = next(r.message for r in caplog.records if r.message.startswith("turn_processed "))
    assert f"turn_id={payload['turn_id']!r}" in line
    for field in ("steward_ms=", "fragments_ms=", "total_ms="):
        assert field in line, line
