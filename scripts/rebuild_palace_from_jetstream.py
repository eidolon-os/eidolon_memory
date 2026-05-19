#!/usr/bin/env python3
"""D5: rebuild a corrupted per-user palace by replaying JetStream history.

Approach:
  1. Ensure no agent_runner is running for the target user (operator's job).
  2. ``mv ~/eidolon/palaces/<user_id>/  ~/eidolon/palaces/<user_id>.corrupted.<ts>``
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
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import nats
from nats.js.api import ConsumerConfig, DeliverPolicy
from pydantic import ValidationError

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.steward import create_steward
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import (
    resolve_palace_for_user,
    validate_user_id,
)
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.infrastructure.bus.subjects import conversation_turn_subject
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
    backend = LockedBackend(MemPalacePythonBackend(settings, str(palace_path)))
    steward = create_steward(settings)
    subject = conversation_turn_subject(user_id)

    nc = await nats.connect(settings.nats.url)
    js = nc.jetstream()
    await ensure_memory_stream(js, settings)

    psub = await js.pull_subscribe(
        subject,
        durable=consumer_name,
        stream=settings.nats.stream,
        config=ConsumerConfig(
            deliver_policy=DeliverPolicy.ALL,
            durable_name=consumer_name,
        ),
    )

    print(
        f"[rebuild] subscribed: stream={settings.nats.stream} "
        f"subject={subject} consumer={consumer_name}"
    )

    processed = 0
    skipped = 0
    invalid = 0
    last_msg_at = time.monotonic()

    try:
        while True:
            try:
                msgs = await psub.fetch(16, timeout=2.0)
            except TimeoutError:
                msgs = []

            if msgs:
                last_msg_at = time.monotonic()
                for msg in msgs:
                    try:
                        raw = json.loads(msg.data.decode("utf-8"))
                        turn = ConversationTurnPayload.model_validate(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
                        invalid += 1
                        print(f"[rebuild][WARN] invalid payload skipped: {exc}")
                        await msg.ack()
                        continue

                    if turn.user_id and turn.user_id != user_id:
                        # subject filter should have prevented this; defensive
                        skipped += 1
                        await msg.ack()
                        continue

                    if dry_run:
                        processed += 1
                        await msg.ack()
                        continue

                    try:
                        await steward.handle_turn(turn, backend)
                        processed += 1
                    except Exception as exc:
                        print(
                            f"[rebuild][ERROR] steward failed on turn "
                            f"{turn.turn_id!r}: {exc}"
                        )
                        # don't ack — let JetStream redeliver on the next pass
                        continue
                    await msg.ack()
            else:
                if time.monotonic() - last_msg_at > idle_timeout:
                    break
    finally:
        try:
            await nc.drain()
        except Exception:
            pass

    print(
        f"[rebuild] done: replayed={processed} skipped_other_user={skipped} "
        f"invalid={invalid}"
    )
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
        args.consumer_name
        or f"rebuild-{user_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
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
