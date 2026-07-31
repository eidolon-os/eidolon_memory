"""LiveKit recall fail-fast and voice filter (D1: direct backend, no PalaceReadSession)."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.livekit_recall import LiveKitRecallService
from eidolon.memory.application.recall_filters import filter_voice_recall_hits
from eidolon.memory.config.memory_settings import (
    MemorySettings,
    RecallPolicy,
)
from eidolon.memory.domain.errors import MemoryBackendUnavailable
from eidolon.memory.domain.wire import MemoryWireRecord


def _settings() -> MemorySettings:
    return MemorySettings(
        recall=RecallPolicy(
            livekit_timeout_seconds=0.2,
            voice_wings=["Wing_Profile"],
            exclude_current_session=True,
            exclude_recent_minutes=0,
        ),
    )


def _ctx(session_id: str = "s1") -> MemoryActorContext:
    return MemoryActorContext(
        memory_realm_id="default.alice.default",
        owner_id="alice",
        companion_id="default",
        device_id="device",
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_livekit_recall_fail_fast_on_error() -> None:
    backend = MagicMock()
    import eidolon.memory.application.livekit_recall as mod

    async def _boom(*_a, **_k):
        raise MemoryBackendUnavailable("Error finding id")

    original = mod.recall_with_kg_fusion
    mod.recall_with_kg_fusion = _boom
    try:
        svc = LiveKitRecallService(backend, _settings(), palace_path="/tmp/fake")
        out = await svc.recall_context_with_records("hello", context=_ctx())
        assert out["context"] == ""
        assert out["degraded"] is True
    finally:
        mod.recall_with_kg_fusion = original


@pytest.mark.asyncio
async def test_livekit_recall_fail_fast_on_timeout() -> None:
    """``asyncio.wait_for`` should expire and degrade rather than hang."""
    import asyncio

    backend = MagicMock()
    import eidolon.memory.application.livekit_recall as mod

    async def _slow(*_a, **_k):
        await asyncio.sleep(5)
        return []

    original = mod.recall_with_kg_fusion
    mod.recall_with_kg_fusion = _slow
    try:
        svc = LiveKitRecallService(backend, _settings(), palace_path="/tmp/fake")
        out = await svc.recall_context_with_records("hello", context=_ctx())
        assert out["degraded"] is True
        assert out["context"] == ""
    finally:
        mod.recall_with_kg_fusion = original


@pytest.mark.asyncio
async def test_livekit_timeout_keeps_realm_serialized_until_worker_finishes() -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()
    next_read_entered = asyncio.Event()

    class SlowScopedFake(FakeMemoryBackend):
        supports_scoped_search = True

        async def search_scoped(self, *_args, **_kwargs):
            def _blocking_search():
                worker_started.set()
                release_worker.wait(timeout=2.0)
                return []

            return await asyncio.to_thread(_blocking_search)

        async def get_all(self, *args, **kwargs):
            next_read_entered.set()
            return await super().get_all(*args, **kwargs)

    backend = LockedBackend(SlowScopedFake())
    settings = _settings()
    settings.recall.livekit_timeout_seconds = 0.05
    service = LiveKitRecallService(backend, settings, palace_path="/tmp/fake")

    recall = asyncio.create_task(service.recall_context_with_records("hello", context=_ctx()))
    assert await asyncio.to_thread(worker_started.wait, 1.0)
    outcome = await recall
    assert outcome["degraded"] is True

    next_read = asyncio.create_task(backend.get_all("default.alice.default"))
    await asyncio.sleep(0.05)
    assert not next_read_entered.is_set()

    release_worker.set()
    assert await next_read == []


def test_filter_excludes_same_session() -> None:
    settings = _settings()
    hits = [
        MemoryWireRecord(
            memory_space_id="Wing_Profile",
            key="k1",
            value="same",
            metadata={"session_id": "sess-a"},
        ),
        MemoryWireRecord(
            memory_space_id="Wing_Profile",
            key="k2",
            value="old",
            metadata={"session_id": "other"},
        ),
    ]
    out = filter_voice_recall_hits(hits, settings, session_id="sess-a")
    assert len(out) == 1
    assert out[0].value == "old"
