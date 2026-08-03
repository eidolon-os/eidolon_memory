#!/usr/bin/env python3
"""W-01: JetStream publish → agent_runner ingest → recall-visible latency (D1).

For each turn:
  * generate a unique token + ConversationTurnPayload for ``--memory-space-id``
  * publish to ``eidolon.memory.turn.<memory_space_id>``
  * record publish-ack latency
  * poll the agent_runner's MCP ``eidolon_memory_get_by_source_turn`` until
    the drawer with ``metadata.source_turn_id == turn_id`` shows up; record end-to-end
    (publish → backend-visible) latency.

End-to-end includes steward LLM time (large + variable). The benchmark reports
publish-ack and e2e separately so both can be inspected.

The visibility check deliberately uses exact lookup by ``source_turn_id``
instead of semantic recall by random token or bounded list scans. That keeps
W-01 focused on JetStream → agent ingest → backend readability; recall ranking
quality is covered by R-01 and B-03.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from eidolon_memory_contracts import MemoryActorContext

from scripts.benchmark.preflight import require_nats  # noqa: E402
from scripts.benchmark.report import percentiles, sla_pass  # noqa: E402


def _local_http_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False)


async def _wait_for_visible(
    sess: ClientSession,
    *,
    turn_id: str,
    deadline: float,
) -> float | None:
    """Poll exact lookup until ``source_turn_id`` is visible."""
    while time.monotonic() < deadline:
        try:
            res = await sess.call_tool(
                "eidolon_memory_get_by_source_turn",
                arguments={
                    "source_turn_id": turn_id,
                },
            )
        except Exception:
            await asyncio.sleep(0.5)
            continue
        if res.content:
            text = res.content[0].text or "{}"
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            if data.get("record") is not None:
                return time.monotonic()
        await asyncio.sleep(0.4)
    return None


async def _run(
    *,
    count: int,
    context: MemoryActorContext,
    nats_url: str,
    stream: str,
    mcp_url: str,
    e2e_timeout_seconds: float,
) -> dict:
    import nats

    from eidolon_memory_contracts import (
        ConversationTurnPayload,
        conversation_turn_subject,
        envelope_memory_payload,
    )

    subject = conversation_turn_subject(context.memory_space_id)
    publish_ms: list[float] = []
    e2e_ms: list[float] = []
    timeouts = 0

    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    async with streamablehttp_client(mcp_url, httpx_client_factory=_local_http_client) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as sess:
            await sess.initialize()

            for i in range(count):
                token = f"bench-w-{uuid.uuid4().hex[:10]}"
                turn_id = uuid.uuid4().hex
                turn = ConversationTurnPayload(
                    turn_id=turn_id,
                    context=context.model_copy(update={"session_id": "bench-w"}),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    user_text=(
                        f"我最近一直在听 Acquired 这档播客，特别是关于半导体的那几期。"
                        f"标记 {token}。"
                    ),
                    assistant_text="记下了。",
                    metadata={"source": "bench_write_jetstream", "iter": i},
                )
                envelope = envelope_memory_payload(turn, trace_id=turn_id)
                body = json.dumps(
                    envelope.model_dump(mode="json"), ensure_ascii=False
                ).encode("utf-8")

                t0 = time.perf_counter()
                ack = await js.publish(subject, body)
                publish_ms.append((time.perf_counter() - t0) * 1000.0)
                del ack  # not used

                t1 = time.monotonic()
                deadline = t1 + e2e_timeout_seconds
                got = await _wait_for_visible(
                    sess,
                    turn_id=turn_id,
                    deadline=deadline,
                )
                if got is None:
                    timeouts += 1
                else:
                    e2e_ms.append((got - t1) * 1000.0)

    await nc.drain()

    pub = percentiles(publish_ms)
    e2e = percentiles(e2e_ms)
    ok = (
        timeouts == 0
        and e2e.get("count", 0) == count
        and sla_pass(pub.get("p50", 9e9), pub.get("p95", 9e9), p50_max=20, p95_max=50)
        and e2e.get("p95", 9e9) <= 10000
    )
    return {
        "id": "W-01",
        "n": count,
        "memory_space_id": context.memory_space_id,
        "subject": subject,
        "timeouts": timeouts,
        "publish_ms": pub,
        "e2e_visible_ms": e2e,
        "sla_publish_p95_ms": 50,
        "sla_e2e_visible_p95_ms": 10000,  # steward LLM dominated; conservative
        "sla": "PASS" if ok else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--owner-user-id", default="bench")
    parser.add_argument("--persona-id", default="mochi")
    parser.add_argument("--agent-id", default="agent-bench")
    parser.add_argument("--device-id", default="bench-device")
    parser.add_argument("--instance-id", default="bench-runtime")
    parser.add_argument(
        "--mcp-url",
        default="http://127.0.0.1:8030/mcp",
        help="agent_runner control-plane MCP URL",
    )
    parser.add_argument(
        "--e2e-timeout-seconds",
        type=float,
        default=30.0,
        help="Per-turn polling deadline for 'write visible via recall'.",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    from eidolon.memory.config.memory_settings import get_memory_settings

    s = get_memory_settings()
    # Before the event loop opens a connection, so a missing broker reads as a
    # missing broker rather than as a client error inside _run.
    require_nats(s.nats.url)
    context = MemoryActorContext(
        tenant_id=args.tenant_id,
        owner_user_id=args.owner_user_id,
        persona_id=args.persona_id,
        agent_id=args.agent_id,
        device_id=args.device_id,
        instance_id=args.instance_id,
        session_id="bench-w",
    )
    row = asyncio.run(
        _run(
            count=args.count,
            context=context,
            nats_url=s.nats.url,
            stream=s.nats.stream,
            mcp_url=args.mcp_url,
            e2e_timeout_seconds=args.e2e_timeout_seconds,
        )
    )
    print(json.dumps(row, indent=2, ensure_ascii=False))

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if row["sla"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
