from __future__ import annotations

from pathlib import Path

import pytest
from eidolon_memory_contracts import MemoryIntent

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.materialization import inspect_materialization
from eidolon.memory.domain.space_runtime import MemorySpaceRuntime, SpaceLedgers
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger

pytestmark = pytest.mark.asyncio

SPACE = "realm-owner-one"


def _intent() -> MemoryIntent:
    return MemoryIntent(
        intent_id="intent-tea",
        memory_space_id=SPACE,
        source_event_id="turn-tea",
        authority="extracted_user",
        intent_type="preference",
        raw_claim="用户喜欢乌龙茶",
        subject="self",
        predicate="likes",
        object="乌龙茶",
        attributes={"audience": "companion:c-one"},
    )


def _runtime(backend, ledger: CanonicalFactLedger | None) -> MemorySpaceRuntime:
    return MemorySpaceRuntime(
        space_id=SPACE,
        backend=backend,
        palace_path="/tmp/not-inspected",
        ledgers=SpaceLedgers(canonical_facts=ledger),
    )


async def test_status_tracks_projection_materialization_not_process_liveness(
    tmp_path: Path,
) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    backend = FakeMemoryBackend()
    registration = await ledger.register(_intent(), targets={"drawer", "kg"})

    pending = await inspect_materialization(_runtime(backend, ledger))

    assert pending.ready is False
    assert pending.details["data_readable"] is True
    assert pending.details["materialization_state"] == "materializing"
    assert pending.details["projection_pending"] == 2
    assert pending.details["last_materialized_at"] is None

    await ledger.mark_projected(SPACE, registration.assertion_id, targets={"drawer", "kg"})
    ready = await inspect_materialization(_runtime(backend, ledger))

    assert ready.ready is True
    assert ready.details["materialization_state"] == "ready"
    assert ready.details["projection_pending"] == 0
    assert ready.details["last_materialized_at"]


async def test_running_shape_cannot_mask_unreadable_data(tmp_path: Path) -> None:
    class Unreadable(FakeMemoryBackend):
        async def get_all(self, *_args, **_kwargs):
            raise RuntimeError("chroma unavailable")

    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")

    status = await inspect_materialization(_runtime(Unreadable(), ledger))

    assert status.ready is False
    assert status.details["data_readable"] is False
    assert status.details["materialization_state"] == "unavailable"
    assert "chroma unavailable" in status.details["degraded_reason"]


async def test_unrequested_projection_is_not_reported_as_backlog(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    backend = FakeMemoryBackend()
    registration = await ledger.register(_intent(), targets={"drawer"})

    await ledger.mark_projected(SPACE, registration.assertion_id, targets={"drawer"})
    status = await inspect_materialization(_runtime(backend, ledger))

    assert status.ready is True
    assert status.details["projection_pending"] == 0
    assert status.details["last_materialized_at"]

    second = _intent().model_copy(
        update={"intent_id": "intent-tea-kg", "source_event_id": "turn-tea-kg"}
    )
    later = await ledger.register(second, targets={"drawer", "kg"})
    status = await inspect_materialization(_runtime(backend, ledger))

    assert later.pending_targets == ["kg"]
    assert status.ready is False
    assert status.details["projection_pending"] == 1


async def test_forget_tracks_only_required_projection_targets(tmp_path: Path) -> None:
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    backend = FakeMemoryBackend()
    registration = await ledger.register(_intent(), targets={"drawer"})
    await ledger.mark_projected(SPACE, registration.assertion_id, targets={"drawer"})

    forgotten = await ledger.begin_forget(
        SPACE,
        [registration.assertion_id],
        hard=True,
        reason="privacy",
        targets={"drawer"},
    )

    assert [plan.assertion_id for plan in forgotten] == [registration.assertion_id]
    pending = await inspect_materialization(_runtime(backend, ledger))
    assert pending.ready is False
    assert pending.details["projection_pending"] == 1

    await ledger.mark_forget_projected(
        SPACE,
        [plan.assertion_id for plan in forgotten],
        targets={"drawer"},
    )
    ready = await inspect_materialization(_runtime(backend, ledger))
    assert ready.ready is True
    assert ready.details["projection_pending"] == 0
    assert ready.details["last_materialized_at"]

    repeated = await ledger.begin_forget(
        SPACE,
        [registration.assertion_id],
        hard=True,
        reason="privacy replay",
        targets={"drawer"},
    )
    replay_status = await inspect_materialization(_runtime(backend, ledger))
    assert [plan.assertion_id for plan in repeated] == [registration.assertion_id]
    assert replay_status.ready is True
    assert replay_status.details["projection_pending"] == 0


async def test_unreadable_knowledge_graph_is_not_ready(tmp_path: Path) -> None:
    class UnreadableGraph:
        async def stats(self):
            raise RuntimeError("kg unavailable")

    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    runtime = MemorySpaceRuntime(
        space_id=SPACE,
        backend=FakeMemoryBackend(),
        palace_path="/tmp/not-inspected",
        kg=UnreadableGraph(),
        ledgers=SpaceLedgers(canonical_facts=ledger),
    )

    status = await inspect_materialization(runtime)

    assert status.ready is False
    assert status.details["data_readable"] is False
    assert status.details["materialization_state"] == "unavailable"
    assert "kg unavailable" in status.details["degraded_reason"]
