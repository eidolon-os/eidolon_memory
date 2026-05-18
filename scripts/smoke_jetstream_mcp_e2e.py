#!/usr/bin/env python3
"""Publish synthetic JetStream turns, then recall via MCP Streamable HTTP tools."""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import datetime, timezone

from eidolon.memory.config.memory_settings import get_memory_settings, reset_memory_settings_cache
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.infrastructure.mcp_http_client import call_tool_json, eidolon_memory_mcp_http_session
from eidolon.memory.infrastructure.nats.turns import JetStreamTurnPublisher


async def _publish_turns(marker: str, user_id: str, session_id: str) -> None:
    reset_memory_settings_cache()
    settings = get_memory_settings()
    publisher = JetStreamTurnPublisher.from_memory_settings(settings)
    await publisher.connect()
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


async def _mcp_http_search(marker: str, user_id: str, top_k: int) -> list:
    """Call ``eidolon_memory_search`` on the running MCP HTTP server."""
    reset_memory_settings_cache()
    async with eidolon_memory_mcp_http_session() as session:
        payload = await call_tool_json(
            session,
            "eidolon_memory_search",
            {
                "query": marker,
                "user_id": user_id,
                "top_k": top_k,
                "wing": None,
                "room": None,
            },
        )
    if not isinstance(payload, list):
        msg = f"unexpected MCP search payload: {type(payload)}"
        raise TypeError(msg)
    return payload


async def main_async(args: argparse.Namespace) -> None:
    marker = args.marker or f"E2E_{uuid.uuid4().hex[:12]}"
    user_id = args.user_id
    session_id = args.session_id
    print("marker:", marker)
    print("user_id:", user_id)
    settings = get_memory_settings()
    print("mcp_http:", settings.mcp_http.base_url())

    if not args.skip_publish:
        await _publish_turns(marker, user_id, session_id)
        print(f"sleep {args.wait_s}s for worker + steward…")
        await asyncio.sleep(args.wait_s)

    records = await _mcp_http_search(marker, user_id, args.top_k)
    print("\n--- MCP HTTP search results ---")
    if not records:
        print(
            "NO HITS (check worker logs, palace path, NATS JetStream, "
            "steward/LLM, and deploy/dev/run_all.sh for MCP HTTP)"
        )
        raise SystemExit(1)
    for i, rec in enumerate(records, start=1):
        print(f"\n[{i}] key={rec.get('key')}")
        print(f"value={rec.get('value')}")
        print(f"metadata={rec.get('metadata')}")
    print("\nOK: found", len(records), "record(s) containing query")


def main() -> None:
    if not os.environ.get("EIDOLON_MEMORY_SETTINGS_YAML", "").strip():
        print(
            "set EIDOLON_MEMORY_SETTINGS_YAML, or create memory.default.yaml "
            "next to memory.default.yaml.example"
        )
        raise SystemExit(2)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", default="", help="Unique substring to search for")
    parser.add_argument("--user-id", default="smoke_e2e_user")
    parser.add_argument("--session-id", default="smoke_e2e_session")
    parser.add_argument("--wait-s", type=float, default=35.0, help="Seconds after publish before search")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--skip-publish", action="store_true", help="Only run MCP HTTP search")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
