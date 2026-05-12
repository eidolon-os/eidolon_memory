"""MemoryService handlers with a fake backend and stub broker."""

from __future__ import annotations

import pytest

from eidolon.memory.infrastructure.bus import BusEnvelope, BusHeader, SharedSubjects
from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.domain.payloads import MemoryStorePayload


class _FakeBroker:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.published: list[tuple[str, dict[str, object]]] = []

    def subscriber(self, subject: str):
        def deco(fn: object) -> object:
            self.handlers[subject] = fn
            return fn

        return deco

    async def publish(self, subject: str, body: dict[str, object]) -> None:
        self.published.append((subject, body))


@pytest.mark.asyncio
async def test_memory_service_does_not_subscribe_memory_query():
    bus = _FakeBroker()
    backend = FakeMemoryBackend()
    svc = MemoryService(bus_broker=bus, backend=backend)
    await svc.start()
    assert SharedSubjects.MEMORY_QUERY not in bus.handlers


@pytest.mark.asyncio
async def test_on_store_roundtrip():
    bus = _FakeBroker()
    backend = FakeMemoryBackend()
    svc = MemoryService(bus_broker=bus, backend=backend)
    await svc.start()

    store_body = BusEnvelope(
        header=BusHeader(source="t"),
        payload=MemoryStorePayload(
            text="hello world",
            wing="alice",
            room="session1",
            correlation_id="c1",
        ).model_dump(),
    ).model_dump()

    await bus.handlers[SharedSubjects.MEMORY_STORE](store_body, reply_to=None)
    assert backend.ingests
    wing, room, text, _meta = backend.ingests[-1]
    assert wing == "alice"
    assert room == "session1"
    assert "hello" in text
