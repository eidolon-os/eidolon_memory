"""Memory Worker — JetStream consumer → steward → MemPalace Python backend."""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nats
from pydantic import ValidationError

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.steward import create_steward
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.infrastructure.bus.subjects import SharedSubjects
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream
from eidolon.memory.infrastructure.palace_generation import (
    bump_generation,
    resolve_generation_path,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


async def process_turn_message(
    msg: Any,
    *,
    steward: Any,
    backend: MemPalacePythonBackend,
    gen_path: Any,
    settings: Any,
    max_deliveries: int,
) -> None:
    """Handle one JetStream message: steward → bump generation → ACK (or DLQ)."""
    deliveries = _delivery_count(msg)
    try:
        raw = json.loads(msg.data.decode("utf-8"))
        turn = ConversationTurnPayload.model_validate(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        log.error("memory_worker_bad_payload", error=str(exc))
        await msg.ack()
        return
    try:
        await steward.handle_turn(turn, backend)
        info = bump_generation(gen_path, writer="eidolon-memory-worker")
        await msg.ack()
        log.debug(
            "memory_worker_turn_acked",
            generation=info.generation,
            turn_id=turn.turn_id,
        )
    except Exception as exc:
        log.error(
            "memory_worker_turn_failed",
            error=str(exc),
            deliveries=deliveries,
        )
        if deliveries >= max_deliveries:
            _append_dlq(settings, msg.data, str(exc), deliveries)
            await msg.ack()
            log.error("memory_worker_dlq_ack", deliveries=deliveries)
        else:
            await msg.nak()


def _append_dlq(settings: Any, payload: bytes, error: str, deliveries: int) -> None:
    path = Path(settings.nats.dlq_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "deliveries": deliveries,
        "payload_preview": payload[:500].decode("utf-8", errors="replace"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _delivery_count(msg: Any) -> int:
    meta = getattr(msg, "metadata", None)
    if meta is None:
        return 1
    return int(getattr(meta, "num_delivered", None) or 1)


async def run_memory_worker(
    *,
    nats_url: str | None = None,
    steward: Any | None = None,
) -> None:
    """Consume ``ConversationTurnPayload`` messages; bump generation before ACK."""
    settings = get_memory_settings()
    url = nats_url or settings.nats.url
    stream = settings.nats.stream
    subject = settings.nats.subject or SharedSubjects.MEMORY_CONVERSATION_TURN
    durable = settings.nats.durable
    max_deliveries = settings.nats.worker_max_deliveries

    palace = str(resolve_palace_directory(settings))
    gen_path = resolve_generation_path(palace, settings.runtime.read.generation_path)
    backend = MemPalacePythonBackend(settings, palace)
    steward_runner = steward or create_steward(settings)

    nc = await nats.connect(url)
    js = nc.jetstream()
    await ensure_memory_stream(js, settings)
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
                await process_turn_message(
                    msg,
                    steward=steward_runner,
                    backend=backend,
                    gen_path=gen_path,
                    settings=settings,
                    max_deliveries=max_deliveries,
                )
    finally:
        await nc.drain()
        log.info("memory_worker_stopped")


def main() -> None:
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env

    apply_cpu_thread_env(get_memory_settings(), role="worker")
    asyncio.run(run_memory_worker())


if __name__ == "__main__":
    main()
