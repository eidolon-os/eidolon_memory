#!/usr/bin/env python3
"""LiveKit-style in-process recall benchmarks (R-01..R-05)."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.benchmark.report import percentiles, sla_pass  # noqa: E402


async def _run(settings, palace: str, *, duration: float, qps: float) -> dict[str, Any]:
    from eidolon.memory.application.livekit_recall import LiveKitRecallService
    from eidolon.memory.application.runtime_warm import warm_palace_read_path
    from eidolon.memory.infrastructure.palace_read_session import PalaceReadSession

    await warm_palace_read_path(settings, palace)
    session = PalaceReadSession(settings, palace)
    svc = LiveKitRecallService(session, settings)

    interval = 1.0 / qps if qps > 0 else 0.5
    end = time.monotonic() + duration
    latencies: list[float] = []
    timeouts = 0
    degraded = 0
    n = 0

    while time.monotonic() < end:
        t0 = time.perf_counter()
        result = await svc.recall_context_with_records(
            "用户最近情绪如何",
            user_id="bench",
            session_id="bench-session",
        )
        ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(ms)
        n += 1
        if result.get("degraded"):
            degraded += 1
        if ms > settings.recall.livekit_timeout_seconds * 1000:
            timeouts += 1
        await asyncio.sleep(max(0, interval - ms / 1000.0))

    stats = percentiles(latencies)
    p50 = stats["p50"]
    p95 = stats["p95"]
    return {
        "id": "R-01",
        "n": n,
        "stats": stats,
        "timeouts": timeouts,
        "degraded": degraded,
        "sla": "PASS"
        if sla_pass(p50, p95, p50_max=150, p95_max=400)
        else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--qps", type=float, default=2.0)
    parser.add_argument("--palace")
    args = parser.parse_args()

    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.config.palace_directory import resolve_palace_directory

    settings = get_memory_settings()
    palace = args.palace or str(resolve_palace_directory(settings))
    row = asyncio.run(_run(settings, palace, duration=args.duration, qps=args.qps))
    print(row)
    return 0 if row["sla"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
