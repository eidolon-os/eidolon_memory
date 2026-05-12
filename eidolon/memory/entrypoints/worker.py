"""Memory Worker — JetStream consumer → steward → MCP ``MemoryBackend``."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

import nats
from nats.js.api import RetentionPolicy, StorageType, StreamConfig

from eidolon.memory.infrastructure.bus.subjects import SharedSubjects
from eidolon.memory.adapters.mempalace_backend import McpMemPalaceBackend
from eidolon.memory.application.steward.noop import NoOpSteward
from eidolon.memory.config.ontology import load_ontology
from eidolon.memory.config.palace_path import resolve_palace_path
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.infrastructure.mcp.config import McpServerLaunchConfig
from eidolon.memory.infrastructure.mcp.runtime import MemPalaceMcpRuntime
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def _stream_subject_durable() -> tuple[str, str, str]:
    stream = os.environ.get("EIDOLON_MEMORY_JS_STREAM", "MEMORY_TURNS").strip()
    subject = os.environ.get(
        "EIDOLON_MEMORY_JS_SUBJECT",
        SharedSubjects.MEMORY_CONVERSATION_TURN,
    ).strip()
    durable = os.environ.get("EIDOLON_MEMORY_JS_DURABLE", "eidolon-memory-worker").strip()
    return stream, subject, durable


async def _ensure_stream(js: Any, stream: str, subject: str) -> None:
    try:
        await js.stream_info(stream)
    except Exception:
        await js.add_stream(
            StreamConfig(
                name=stream,
                subjects=[subject],
                retention=RetentionPolicy.LIMITS,
                storage=StorageType.FILE,
                max_age=86400 * 14,
            )
        )
        log.info("worker_stream_created", stream=stream, subject=subject)


async def run_memory_worker(
    *,
    nats_url: str | None = None,
    steward: Any | None = None,
) -> None:
    """Consume ``ConversationTurnPayload`` messages and ACK after successful MCP writes."""
    url = nats_url or os.environ.get("NATS_URL", "nats://localhost:4222")
    stream, subject, durable = _stream_subject_durable()

    launch = McpServerLaunchConfig.from_environ()
    if not launch.is_configured():
        msg = "EIDOLON_MEMORY_MCP_COMMAND must be set for memory worker"
        raise RuntimeError(msg)

    runtime = MemPalaceMcpRuntime(launch)
    await runtime.start()
    ontology = load_ontology()
    palace = str(resolve_palace_path())
    backend = McpMemPalaceBackend(runtime, ontology, palace)
    steward_runner = steward or NoOpSteward()

    nc = await nats.connect(url)
    js = nc.jetstream()
    await _ensure_stream(js, stream, subject)
    psub = await js.pull_subscribe(subject, durable=durable, stream=stream)

    stop = asyncio.Event()

    def _sig(*_: Any) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig)
        except NotImplementedError:
            pass

    log.info("memory_worker_pull_subscribe", stream=stream, subject=subject, durable=durable)

    try:
        while not stop.is_set():
            try:
                msgs = await psub.fetch(8, timeout=2.0)
            except TimeoutError:
                continue
            for msg in msgs:
                try:
                    raw = json.loads(msg.data.decode("utf-8"))
                    turn = ConversationTurnPayload.model_validate(raw)
                    await steward_runner.handle_turn(turn, backend)
                    await msg.ack()
                except Exception as exc:
                    log.error("memory_worker_turn_failed", error=str(exc))
                    await msg.nak()
    finally:
        await runtime.stop()
        await nc.drain()
        log.info("memory_worker_stopped")


def main() -> None:
    asyncio.run(run_memory_worker())


if __name__ == "__main__":
    main()
