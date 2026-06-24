#!/usr/bin/env python3
"""R-01: end-to-end recall latency through a running agent_runner (D1).

The benchmark targets the same code path LiveKit voice pipelines use in
production: an MCP ``streamable-http`` client calls
``eidolon_memory_recall_context`` on a single-user agent_runner; each call is
gated by the user's ``asyncio.Lock`` and hits the same chromadb
PersistentClient.

The caller is responsible for starting and tearing down the agent_runner
subprocess; this script is a pure load generator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from eidolon_sdk.memory import MemoryActorContext

from scripts.benchmark.report import percentiles, sla_pass  # noqa: E402

_DEFAULT_QUERIES = [
    "用户最近的情绪状态",
    "用户喜欢什么音乐",
    "工作压力",
    "和家人的关系",
    "最近的健康状况",
    "兴趣爱好",
    "重要的事件",
    "生活习惯和偏好",
]


def _local_http_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False)


def _extract_records(call_result) -> list[dict]:
    if not call_result.content:
        return []
    text = call_result.content[0].text or "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data.get("records") or []


async def _run(
    url: str,
    *,
    count: int,
    queries: list[str],
    voice: bool,
    with_kg: bool,
    context: MemoryActorContext,
) -> dict:
    latencies_ms: list[float] = []
    errors = 0
    hit_count = 0
    rng = random.Random(0xC0FFEE)

    # Warm up: first session creates the MCP session, which is dominated by
    # one-time handshake costs; skip its sample.
    async with streamablehttp_client(url, httpx_client_factory=_local_http_client) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            # Re-use a single session to avoid HTTP/MCP setup cost per call;
            # this mirrors how a LiveKit pipeline keeps an MCP client alive.
            warm_q = rng.choice(queries)
            await sess.call_tool(
                "eidolon_memory_recall_context",
                arguments={
                    "query": warm_q,
                    "context": context.model_dump(mode="json"),
                    "top_k": 5,
                    "voice": voice,
                    "include_kg": with_kg,
                },
            )

            for i in range(count):
                q = rng.choice(queries)
                t0 = time.perf_counter()
                try:
                    res = await sess.call_tool(
                        "eidolon_memory_recall_context",
                        arguments={
                            "query": q,
                            "context": context.model_dump(mode="json"),
                            "top_k": 5,
                            "voice": voice,
                            "include_kg": with_kg,
                        },
                    )
                except Exception:
                    errors += 1
                    continue
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                latencies_ms.append(elapsed_ms)
                if _extract_records(res):
                    hit_count += 1

    stats = percentiles(latencies_ms)
    return {
        "id": "R-01",
        "n": count,
        "errors": errors,
        "hit_rate_pct": (
            round(hit_count / max(1, len(latencies_ms)) * 100.0, 1)
            if latencies_ms
            else 0.0
        ),
        "latency_ms": stats,
        "sla_p95_ms": 300,
        "sla_p99_ms": 350,
        "sla": "PASS"
        if sla_pass(stats.get("p50", 9e9), stats.get("p95", 9e9), p50_max=200, p95_max=300)
        else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8030/mcp",
        help="agent_runner control-plane MCP URL (default localhost:8030)",
    )
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--owner-user-id", default="bench")
    parser.add_argument("--persona-id", default="mochi")
    parser.add_argument("--agent-id", default="agent-bench")
    parser.add_argument("--device-id", default="bench-device")
    parser.add_argument("--instance-id", default="bench-runtime")
    parser.add_argument("--session-id", default="bench-session")
    parser.add_argument(
        "--query",
        action="append",
        default=None,
        help="Override query corpus; pass multiple times. Defaults to a built-in 8-query mix.",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="Use the LiveKit hot path (shared query embedding across wings).",
    )
    parser.add_argument(
        "--with-kg",
        action="store_true",
        help="Pass include_kg=true to recall_context (KG plan §5.7 KG-V7 bench).",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Optional JSON output path",
    )
    args = parser.parse_args()

    queries = args.query if args.query else _DEFAULT_QUERIES
    context = MemoryActorContext(
        tenant_id=args.tenant_id,
        owner_user_id=args.owner_user_id,
        persona_id=args.persona_id,
        agent_id=args.agent_id,
        device_id=args.device_id,
        instance_id=args.instance_id,
        session_id=args.session_id,
    )
    row = asyncio.run(
        _run(
            args.url, count=args.count, queries=queries,
            voice=args.voice, with_kg=args.with_kg, context=context,
        )
    )
    row["memory_space_id"] = context.memory_space_id
    row["mode"] = ("voice" if args.voice else "non-voice") + (
        "+kg" if args.with_kg else ""
    )
    print(json.dumps(row, indent=2, ensure_ascii=False))

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if row["sla"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
