"""MemoryService + MemPalace MCP runtime (NATS **writes / CRUD** only)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.mempalace_backend import McpMemPalaceBackend
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.config.ontology import load_ontology
from eidolon.memory.config.palace_path import palace_path_cli_override, resolve_palace_path
from eidolon.memory.infrastructure.bus import BusClient
from eidolon.memory.infrastructure.mcp.config import McpServerLaunchConfig
from eidolon.memory.infrastructure.mcp.runtime import MemPalaceMcpRuntime
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


async def _run(
    service: MemoryService,
    bus: Any,
    runtime: MemPalaceMcpRuntime | None,
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
    if runtime is not None:
        await runtime.stop()


async def _serve_async(nats_url: str, palace_override: str | None) -> None:
    bus = BusClient.create(servers=nats_url)
    launch = McpServerLaunchConfig.from_environ()
    use_fake = os.environ.get("EIDOLON_MEMORY_FAKE_BACKEND", "").lower() in ("1", "true", "yes")

    runtime: MemPalaceMcpRuntime | None = None
    if use_fake:
        log.warning("memory_server_using_fake_backend")
        backend: Any = FakeMemoryBackend()
    elif launch.is_configured():
        runtime = MemPalaceMcpRuntime(launch)
        await runtime.start()
        ont = load_ontology()
        palace = str(resolve_palace_path(palace_override))
        backend = McpMemPalaceBackend(runtime, ont, palace)
    else:
        log.error(
            "memory_server_mcp_missing",
            hint="Set EIDOLON_MEMORY_MCP_COMMAND or EIDOLON_MEMORY_FAKE_BACKEND=1",
        )
        raise SystemExit(1)

    service = MemoryService(bus_broker=bus, backend=backend)
    await _run(service, bus, runtime)


def main() -> None:
    palace_override = palace_path_cli_override()
    raw_url = sys.argv[1] if len(sys.argv) > 1 else "nats://localhost:4222"
    nats_url = raw_url if raw_url else "nats://localhost:4222"
    try:
        asyncio.run(_serve_async(nats_url, palace_override))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
