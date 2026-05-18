#!/usr/bin/env python3
"""JetStream publish → worker ACK latency benchmark (W-01)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.benchmark.report import percentiles, sla_pass  # noqa: E402


async def _run(*, count: int, nats_url: str, subject: str, stream: str) -> dict[str, Any]:
    import nats
    from nats.js.api import DeliverPolicy

    from eidolon.memory.domain.payloads import ConversationTurnPayload

    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    publish_ms: list[float] = []
    e2e_ms: list[float] = []

    for i in range(count):
        from datetime import datetime, timezone

        turn = ConversationTurnPayload(
            turn_id=f"bench-{uuid.uuid4().hex[:12]}",
            session_id="bench-session",
            user_id="bench",
            user_text=f"bench message {i}",
            assistant_text="ok",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        body = json.dumps(turn.model_dump(mode="json")).encode("utf-8")
        t0 = time.perf_counter()
        ack = await js.publish(subject, body)
        publish_ms.append((time.perf_counter() - t0) * 1000.0)
        start = time.perf_counter()
        while time.monotonic() - start < 30.0:
            try:
                info = await js.get_msg(stream, ack.seq)
                if info:
                    e2e_ms.append((time.perf_counter() - t0) * 1000.0)
                    break
            except Exception:
                pass
            await asyncio.sleep(0.05)

    await nc.drain()
    pub = percentiles(publish_ms)
    e2e = percentiles(e2e_ms)
    return {
        "id": "W-01",
        "n": count,
        "publish": pub,
        "e2e_ack": e2e,
        "sla": "PASS"
        if sla_pass(pub["p95"], e2e["p95"], p50_max=10, p95_max=500)
        else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()

    from eidolon.memory.config.memory_settings import get_memory_settings

    s = get_memory_settings()
    row = asyncio.run(
        _run(
            count=args.count,
            nats_url=s.nats.url,
            subject=s.nats.subject,
            stream=s.nats.stream,
        )
    )
    print(row)
    return 0 if row["sla"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
