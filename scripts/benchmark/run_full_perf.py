#!/usr/bin/env python3
"""Generate read/write performance report (metrics.json + summary.md)."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.benchmark.report import percentiles, sla_pass, write_report  # noqa: E402


async def bench_read_livekit(
    settings: Any,
    palace: str,
    *,
    duration: float,
    qps: float,
    scenario_id: str,
) -> dict[str, Any]:
    from eidolon.memory.application.livekit_recall import LiveKitRecallService
    from eidolon.memory.application.runtime_warm import warm_palace_read_path
    from eidolon.memory.infrastructure.palace_read_session import PalaceReadSession

    await warm_palace_read_path(settings, palace)
    session = PalaceReadSession(settings, palace)
    svc = LiveKitRecallService(session, settings)

    interval = 1.0 / qps if qps > 0 else 0.5
    end = time.monotonic() + duration
    latencies: list[float] = []
    degraded = 0
    n = 0

    while time.monotonic() < end:
        t0 = time.perf_counter()
        result = await svc.recall_context_with_records(
            "用户最近情绪如何，什么能让他放松",
            user_id="bench",
            session_id="bench-session",
        )
        ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(ms)
        n += 1
        if result.get("degraded"):
            degraded += 1
        await asyncio.sleep(max(0, interval - ms / 1000.0))

    stats = percentiles(latencies)
    timeout_rate = degraded / n if n else 0.0
    sla = (
        "PASS"
        if sla_pass(stats["p50"], stats["p95"], p50_max=150, p95_max=400)
        and timeout_rate < 0.01
        else "FAIL"
    )
    return {
        "id": scenario_id,
        "n": n,
        "p50": round(stats["p50"], 2),
        "p95": round(stats["p95"], 2),
        "p99": round(stats["p99"], 2),
        "max": round(stats["max"], 2),
        "mean": round(stats["mean"], 2),
        "degraded": degraded,
        "degraded_pct": round(100.0 * timeout_rate, 2),
        "sla": sla,
        "stats": stats,
    }


async def bench_read_single_wing(
    settings: Any,
    palace: str,
    *,
    count: int = 50,
) -> dict[str, Any]:
    from eidolon.memory.application.runtime_warm import warm_palace_read_path
    from eidolon.memory.infrastructure.palace_read_session import PalaceReadSession
    from eidolon.memory.application.public_recall import search_all_wings_mcp_style

    await warm_palace_read_path(settings, palace)
    session = PalaceReadSession(settings, palace)
    backend = await session.active_backend()
    wing = (
        settings.recall.voice_wings[0]
        if settings.recall.voice_wings
        else "Wing_Profile"
    )
    latencies: list[float] = []
    for _ in range(count):
        t0 = time.perf_counter()
        await search_all_wings_mcp_style(
            backend,
            settings,
            query="用户偏好",
            user_id="bench",
            top_k=5,
            wing=wing,
            room=None,
            for_voice=True,
            session_id="bench-session",
            palace_path=palace,
        )
        latencies.append((time.perf_counter() - t0) * 1000.0)
    stats = percentiles(latencies)
    return {
        "id": "R-04",
        "n": count,
        "wing": wing,
        "p50": round(stats["p50"], 2),
        "p95": round(stats["p95"], 2),
        "p99": round(stats["p99"], 2),
        "max": round(stats["max"], 2),
        "sla": "INFO",
        "stats": stats,
    }


async def bench_write_direct_ingest(
    settings: Any,
    palace: str,
    *,
    count: int,
) -> dict[str, Any]:
    from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.infrastructure.palace_generation import read_generation, resolve_generation_path

    backend = MemPalacePythonBackend(settings, palace)
    gen_path = resolve_generation_path(palace, settings.runtime.read.generation_path)
    gen_before = read_generation(gen_path).generation
    latencies: list[float] = []

    for i in range(count):
        frag = MemoryFragment(
            fragment_id=f"bench-{uuid.uuid4().hex[:12]}",
            user_id="bench",
            wing="Wing_Profile",
            room="profile_core",
            content=f"benchmark write sample {i} prefers quiet evenings",
            memory_type="preference",
            importance=4,
            confidence=0.9,
            source_turn_id=f"bench-turn-{i}",
            session_id="bench-session",
        )
        t0 = time.perf_counter()
        await backend.ingest_fragment(frag)
        latencies.append((time.perf_counter() - t0) * 1000.0)

    gen_after = read_generation(gen_path).generation
    stats = percentiles(latencies)
    sla = "PASS" if stats["p95"] < 500 else "FAIL"
    return {
        "id": "W-direct",
        "n": count,
        "p50": round(stats["p50"], 2),
        "p95": round(stats["p95"], 2),
        "p99": round(stats["p99"], 2),
        "max": round(stats["max"], 2),
        "generation_delta": gen_after - gen_before,
        "note": "Worker path (Chroma upsert, no NATS/steward)",
        "sla": sla,
        "stats": stats,
    }


async def bench_write_nats_publish(
    settings: Any,
    *,
    count: int,
) -> dict[str, Any]:
    import nats

    from eidolon.memory.domain.payloads import ConversationTurnPayload

    url = settings.nats.url
    subject = settings.nats.subject
    publish_ms: list[float] = []
    errors = 0

    try:
        nc = await nats.connect(url)
    except Exception as exc:
        return {
            "id": "W-publish",
            "n": 0,
            "sla": "SKIP",
            "note": f"NATS unavailable: {exc}",
        }

    js = nc.jetstream()
    for i in range(count):
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
        try:
            await js.publish(subject, body)
            publish_ms.append((time.perf_counter() - t0) * 1000.0)
        except Exception:
            errors += 1
    await nc.drain()

    stats = percentiles(publish_ms)
    sla = "PASS" if stats.get("p95", 999) < 10 else "FAIL" if publish_ms else "SKIP"
    return {
        "id": "W-publish",
        "n": len(publish_ms),
        "errors": errors,
        "p50": round(stats["p50"], 2),
        "p95": round(stats["p95"], 2),
        "max": round(stats["max"], 2),
        "note": "JetStream publish only (worker ACK not measured)",
        "sla": sla,
        "stats": stats,
    }


async def bench_mixed(
    settings: Any,
    palace: str,
    *,
    duration: float,
) -> dict[str, Any]:
    from eidolon.memory.application.livekit_recall import LiveKitRecallService
    from eidolon.memory.application.runtime_warm import warm_palace_read_path
    from eidolon.memory.infrastructure.palace_read_session import PalaceReadSession
    from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
    from eidolon.memory.domain.fragments import MemoryFragment

    await warm_palace_read_path(settings, palace)
    session = PalaceReadSession(settings, palace)
    recall = LiveKitRecallService(session, settings)
    backend = MemPalacePythonBackend(settings, palace)

    end = time.monotonic() + duration
    read_lat: list[float] = []
    write_lat: list[float] = []
    i = 0
    while time.monotonic() < end:
        t0 = time.perf_counter()
        await recall.recall_context("混合压测查询", user_id="bench", session_id="mix")
        read_lat.append((time.perf_counter() - t0) * 1000.0)
        if i % 10 == 0:
            frag = MemoryFragment(
                fragment_id=f"mix-{uuid.uuid4().hex[:8]}",
                user_id="bench",
                wing="Wing_Life",
                room="preference_hobby",
                content=f"mixed soak write {i}",
                memory_type="life",
                importance=3,
                confidence=0.8,
                source_turn_id=f"mix-{i}",
                session_id="mix-session",
            )
            t1 = time.perf_counter()
            await backend.ingest_fragment(frag)
            write_lat.append((time.perf_counter() - t1) * 1000.0)
        i += 1
        await asyncio.sleep(0.45)

    rs = percentiles(read_lat)
    ws = percentiles(write_lat) if write_lat else {"p50": 0, "p95": 0}
    return {
        "id": "M-01",
        "duration_s": duration,
        "read_n": len(read_lat),
        "read_p50": round(rs["p50"], 2),
        "read_p95": round(rs["p95"], 2),
        "write_n": len(write_lat),
        "write_p50": round(ws["p50"], 2),
        "write_p95": round(ws["p95"], 2),
        "sla": "PASS" if rs["p95"] < 400 else "FAIL",
    }


def _drawer_count(palace: str) -> int:
    try:
        from mempalace.palace import get_collection

        col = get_collection(palace, create=False)
        return int(col.count())
    except Exception:
        return -1


def _git_commit() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=_ROOT,
                text=True,
            )
            .strip()
        )
    except Exception:
        return "n/a"


async def main_async(args: argparse.Namespace) -> int:
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.config.palace_directory import resolve_palace_directory
    from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env

    settings = get_memory_settings()
    omp = apply_cpu_thread_env(settings, role="livekit")
    palace = args.palace or str(resolve_palace_directory(settings))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git": _git_commit(),
        "python": platform.python_version(),
        "machine": platform.platform(),
        "palace": palace,
        "drawer_hint": _drawer_count(palace),
        "OMP_NUM_THREADS": str(omp),
        "shared_query_embedding": settings.runtime.read.shared_query_embedding,
        "voice_skip_closets": settings.runtime.read.voice_skip_closets,
        "fast_path": "all voice wings (incl. single) via shared_query_embedding",
    }

    print("[1/5] Read R-01 LiveKit recall …")
    r01 = await bench_read_livekit(
        settings, palace, duration=args.duration_read, qps=2.0, scenario_id="R-01"
    )
    print("[2/5] Read R-04 single wing …")
    r04 = await bench_read_single_wing(settings, palace, count=args.read_single_count)
    if args.read_only:
        w_direct = {"id": "W-direct", "sla": "SKIP", "note": "read-only run"}
        w_pub = {"id": "W-publish", "sla": "SKIP", "note": "read-only run"}
        mixed = {"id": "M-01", "sla": "SKIP", "note": "read-only run"}
    else:
        print("[3/5] Write W-direct ingest …")
        w_direct = await bench_write_direct_ingest(settings, palace, count=args.write_count)
        print("[4/5] Write W-publish NATS …")
        w_pub = await bench_write_nats_publish(settings, count=args.write_count)
        print("[5/5] Mixed M-01 …")
        mixed = await bench_mixed(settings, palace, duration=args.duration_mixed)

    read_table = [r01, r04]
    write_table = [w_direct, w_pub]
    mixed_table = [mixed]

    sections = {
        "read": {"table": read_table},
        "write": {"table": write_table},
        "mixed": {"table": mixed_table},
    }
    write_report(out_dir, meta, sections)

    (out_dir / "metrics.json").write_text(
        json.dumps(
            {"meta": meta, "read": read_table, "write": write_table, "mixed": mixed_table},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nReport: {out_dir / 'summary.md'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace")
    parser.add_argument("--out", default="reports/memory_perf_latest")
    parser.add_argument("--duration-read", type=float, default=60.0)
    parser.add_argument("--duration-mixed", type=float, default=90.0)
    parser.add_argument("--write-count", type=int, default=30)
    parser.add_argument("--read-single-count", type=int, default=40)
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Only run read benchmarks (R-01, R-04); skip write/mixed.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
