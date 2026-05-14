#!/usr/bin/env python3
"""Publish synthetic JetStream turns, then recall using the same search path as MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import datetime, timezone

from eidolon.memory.config.memory_settings import get_memory_settings, reset_memory_settings_cache
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.entrypoints import mcp_server
from eidolon.memory.infrastructure.nats.turns import JetStreamTurnPublisher


async def _publish_turns(marker: str, user_id: str, session_id: str) -> None:
    reset_memory_settings_cache()
    settings = get_memory_settings()
    publisher = JetStreamTurnPublisher.from_memory_settings(settings)
    turns = [
        (
            f"smoke-a-{uuid.uuid4().hex[:8]}",
            f"我最近常想起我们周末去湖边散步的事，特别是那句「{marker}_A」让我印象很深。",
            "我会把这段温暖回忆当作你情绪里的一个锚点。",
        ),
        (
            f"smoke-b-{uuid.uuid4().hex[:8]}",
            f"工作上「{marker}_B」那个需求变更让我有点烦躁，但你说先拆分任务我就安心多了。",
            "好的，我们之后可以把大需求拆成可交付的小块来减压。",
        ),
    ]
    for turn_id, user_text, assistant_text in turns:
        payload = ConversationTurnPayload(
            turn_id=turn_id,
            user_id=user_id,
            session_id=session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_text=user_text,
            assistant_text=assistant_text,
            metadata={"source": "scripts/smoke_jetstream_mcp_e2e.py", "marker": marker},
        )
        await publisher.publish_turn(payload)
        print("published", turn_id)
    await publisher.close()


async def _mcp_style_search(marker: str, user_id: str, top_k: int) -> list:
    """Same wing fan-out + visibility as ``eidolon_memory_search`` (see mcp_server)."""
    reset_memory_settings_cache()
    mcp_server._backend = None
    records = await mcp_server._search_all_wings(
        query=marker,
        user_id=user_id,
        top_k=top_k,
        wing=None,
        room=None,
    )
    return records


async def main_async(args: argparse.Namespace) -> None:
    marker = args.marker or f"E2E_{uuid.uuid4().hex[:12]}"
    user_id = args.user_id
    session_id = args.session_id
    print("marker:", marker)
    print("user_id:", user_id)

    if not args.skip_publish:
        await _publish_turns(marker, user_id, session_id)
        print(f"sleep {args.wait_s}s for worker + steward…")
        await asyncio.sleep(args.wait_s)

    records = await _mcp_style_search(marker, user_id, args.top_k)
    print("\n--- MCP-style search results ---")
    if not records:
        print("NO HITS (check worker logs, palace path, NATS JetStream, steward/LLM)")
        raise SystemExit(1)
    for i, rec in enumerate(records, start=1):
        print(f"\n[{i}] key={rec.key}")
        print(f"value={rec.value}")
        print(f"metadata={rec.metadata}")
    print("\nOK: found", len(records), "record(s) containing query")


def main() -> None:
    if not os.environ.get("EIDOLON_MEMORY_SETTINGS_YAML", "").strip():
        print("set EIDOLON_MEMORY_SETTINGS_YAML to an absolute path (e.g. memory.default.yaml)")
        raise SystemExit(2)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", default="", help="Unique substring to search for")
    parser.add_argument("--user-id", default="smoke_e2e_user")
    parser.add_argument("--session-id", default="smoke_e2e_session")
    parser.add_argument("--wait-s", type=float, default=35.0, help="Seconds after publish before search")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--skip-publish", action="store_true", help="Only run MCP-style search")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
