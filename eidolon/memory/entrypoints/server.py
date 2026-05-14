"""MemoryService + MemPalace Python backend (NATS **writes / CRUD** only)."""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory
from eidolon.memory.infrastructure.bus import BusClient
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


async def _run(
    service: MemoryService,
    bus: Any,
) -> None:
    await BusClient.start(bus)
    await service.start()
    stop_event = asyncio.Event()

    def handle_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    await stop_event.wait()
    await BusClient.stop(bus)


async def _serve_async(settings: MemorySettings, nats_url: str) -> None:
    bus = BusClient.create(servers=nats_url)

    if settings.runtime.fake_backend:
        log.warning("memory_server_using_fake_backend")
        backend: Any = FakeMemoryBackend()
    else:
        palace = str(resolve_palace_directory(settings))
        backend = MemPalacePythonBackend(settings, palace)

    service = MemoryService(bus_broker=bus, backend=backend)
    await _run(service, bus)


def main() -> None:
    settings = get_memory_settings()
    raw_url = sys.argv[1] if len(sys.argv) > 1 else ""
    nats_url = raw_url if raw_url else settings.nats.url
    try:
        asyncio.run(_serve_async(settings, nats_url))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
