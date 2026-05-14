"""Memory Worker — JetStream consumer → steward → MemPalace Python backend."""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

import nats
from nats.js.api import RetentionPolicy, StorageType, StreamConfig
from pydantic import ValidationError

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.steward import create_steward
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.infrastructure.bus.subjects import SharedSubjects
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


async def _ensure_stream(js: Any, stream: str, subject: str, max_age: int) -> None:
    try:
        await js.stream_info(stream)
    except Exception:
        await js.add_stream(
            StreamConfig(
                name=stream,
                subjects=[subject],
                retention=RetentionPolicy.LIMITS,
                storage=StorageType.FILE,
                max_age=max_age,
            )
        )
        log.info("worker_stream_created", stream=stream, subject=subject)


async def run_memory_worker(
    *,
    nats_url: str | None = None,
    steward: Any | None = None,
) -> None:
    """Consume ``ConversationTurnPayload`` messages and ACK after successful MCP writes."""
    settings = get_memory_settings()
    url = nats_url or settings.nats.url
    stream = settings.nats.stream
    subject = settings.nats.subject or SharedSubjects.MEMORY_CONVERSATION_TURN
    durable = settings.nats.durable

    palace = str(resolve_palace_directory(settings))
    backend = MemPalacePythonBackend(settings, palace)
    steward_runner = steward or create_steward(settings)

    nc = await nats.connect(url)
    js = nc.jetstream()
    await _ensure_stream(js, stream, subject, settings.nats.stream_max_age_seconds)
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
                except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
                    log.error("memory_worker_bad_payload", error=str(exc))
                    await msg.ack()
                    continue
                try:
                    await steward_runner.handle_turn(turn, backend)
                    await msg.ack()
                except Exception as exc:
                    log.error("memory_worker_turn_failed", error=str(exc))
                    await msg.nak()
    finally:
        await nc.drain()
        log.info("memory_worker_stopped")


def main() -> None:
    asyncio.run(run_memory_worker())


if __name__ == "__main__":
    main()
