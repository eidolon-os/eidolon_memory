#!/usr/bin/env python3
"""D5: rebuild a corrupted per-user palace by replaying JetStream history.

Approach:
  1. Ensure no agent_runner is running for the target user (operator's job).
  2. Move ``$EIDOLON_STATE_ROOT/memory/mempalaces/<memory_space_id>`` aside.
  3. ``mempalace init`` a fresh palace at the original path.
  4. Subscribe to JetStream from the head of the stream (one-shot, ``deliver_policy=all``).
  5. For each turn whose ``payload.user_id`` matches the target, run the configured
     steward → backend.ingest_fragment. ``drawer_id = sha256(...)`` keeps writes
     idempotent on replay.
  6. Stop when JetStream signals "no more messages" (configurable idle timeout).

This script must be invoked manually after a corruption event; it is **not** part
of agent_runner's normal lifecycle. See docs/architecture-d1-readwrite-split.md §6 (D5).

Usage::

    uv run python scripts/rebuild_palace_from_jetstream.py \\
        --user-id alice [--dry-run] [--idle-timeout 30] [--consumer-name rebuild-alice-<ts>]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import nats
from eidolon_memory_contracts import conversation_turn_subject
from nats.js.api import ConsumerConfig, DeliverPolicy

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.steward import create_steward
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import (
    resolve_palace_for_user,
    validate_user_id,
)
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized


def _archive_corrupted(palace_path: Path) -> Path | None:
    if not palace_path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = palace_path.parent / f"{palace_path.name}.corrupted.{ts}"
    palace_path.rename(dest)
    print(f"[rebuild] archived {palace_path} → {dest}")
    return dest


async def _replay(
    *,
    user_id: str,
    palace_path: Path,
    settings: Any,
    idle_timeout: float,
    consumer_name: str,
    dry_run: bool,
) -> int:
    """Rebuild both chroma drawers AND KG triples from JetStream history.

    Subscribes to **both** turn and command subjects so admin-issued KG edits
    (which also live in JetStream by design) are recovered identically to
    chat-derived facts. Uses the same ``process_turn_message`` /
    ``process_command_message`` paths the live agent_runner uses.
    """

    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
    from eidolon_memory_contracts import memory_command_subject
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.application.turn_processor import (
        process_command_message,
        process_turn_message,
    )

    backend = LockedBackend(MemPalacePythonBackend(settings, str(palace_path)))
    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(palace_path / "knowledge_graph.sqlite3")),
        backend.lock,
    )
    steward = create_steward(settings)
    turn_subject = conversation_turn_subject(user_id)
    cmd_subject = memory_command_subject(user_id)

    nc = await nats.connect(settings.nats.url)
    js = nc.jetstream()
    await ensure_memory_stream(js, settings)

    psub_turn = await js.pull_subscribe(
        turn_subject,
        durable=f"{consumer_name}-turn",
        stream=settings.nats.stream,
        config=ConsumerConfig(
            deliver_policy=DeliverPolicy.ALL,
            durable_name=f"{consumer_name}-turn",
        ),
    )
    psub_cmd = await js.pull_subscribe(
        cmd_subject,
        durable=f"{consumer_name}-cmd",
        stream=settings.nats.stream,
        config=ConsumerConfig(
            deliver_policy=DeliverPolicy.ALL,
            durable_name=f"{consumer_name}-cmd",
        ),
    )

    print(
        f"[rebuild] subscribed: stream={settings.nats.stream} "
        f"turn_subject={turn_subject} cmd_subject={cmd_subject}"
    )

    processed = 0
    skipped = 0
    invalid = 0
    last_msg_at = time.monotonic()

    async def _replay_turn(msg):
        nonlocal processed, invalid
        if dry_run:
            await msg.ack()
            processed += 1
            return
        try:
            await process_turn_message(
                msg,
                steward=steward,
                backend=backend,
                kg=kg,
                settings=settings,
                max_deliveries=settings.nats.worker_max_deliveries,
                expected_user_id=user_id,
            )
            processed += 1
        except Exception as exc:
            invalid += 1
            print(f"[rebuild][ERROR] turn apply failed: {exc}")

    async def _replay_cmd(msg):
        nonlocal processed
        if dry_run:
            await msg.ack()
            processed += 1
            return
        await process_command_message(
            msg,
            backend=backend,
            kg=kg,
            settings=settings,
            expected_user_id=user_id,
        )
        processed += 1

    try:
        while True:
            try:
                msgs_turn = await psub_turn.fetch(16, timeout=1.0)
            except TimeoutError:
                msgs_turn = []
            try:
                msgs_cmd = await psub_cmd.fetch(16, timeout=1.0)
            except TimeoutError:
                msgs_cmd = []

            any_progress = False
            if msgs_turn:
                last_msg_at = time.monotonic()
                any_progress = True
                for msg in msgs_turn:
                    await _replay_turn(msg)
            if msgs_cmd:
                last_msg_at = time.monotonic()
                any_progress = True
                for msg in msgs_cmd:
                    await _replay_cmd(msg)

            if not any_progress:
                if time.monotonic() - last_msg_at > idle_timeout:
                    break
                continue
    finally:
        try:
            await nc.drain()
        except Exception:
            pass
        try:
            kg.close()
        except Exception:
            pass

    print(f"[rebuild] done: replayed={processed} skipped_other_user={skipped} invalid={invalid}")
    return processed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rebuild_palace_from_jetstream",
        description=__doc__,
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=30.0,
        help="Seconds of no new messages before declaring replay complete (default 30s).",
    )
    parser.add_argument(
        "--consumer-name",
        default="",
        help="Override the JetStream durable name (default rebuild-<user>-<ts>).",
    )
    parser.add_argument(
        "--keep-current",
        action="store_true",
        help="Do not archive the existing palace dir (caller already moved it).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate payloads + count messages without writing to chromadb.",
    )
    args = parser.parse_args(argv)

    settings = get_memory_settings()
    user_id = validate_user_id(args.user_id)
    palace_path = resolve_palace_for_user(settings, user_id)

    if not args.keep_current:
        _archive_corrupted(palace_path)
    else:
        if palace_path.exists():
            print(
                f"[rebuild][ERROR] --keep-current set but {palace_path} still exists "
                f"(archive or remove it first)"
            )
            return 2

    ensure_palace_initialized(user_id, palace_path)
    print(f"[rebuild] fresh palace ready: {palace_path}")

    consumer_name = (
        args.consumer_name or f"rebuild-{user_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    try:
        asyncio.run(
            _replay(
                user_id=user_id,
                palace_path=palace_path,
                settings=settings,
                idle_timeout=args.idle_timeout,
                consumer_name=consumer_name,
                dry_run=args.dry_run,
            )
        )
    except KeyboardInterrupt:
        print("[rebuild] interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
