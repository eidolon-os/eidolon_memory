#!/usr/bin/env python3
"""Multi-device memory architecture benchmark.

Produces B-01/B-02/B-03/B-04 style deterministic reports without requiring
NATS, Chroma, or an LLM. It exercises the policy, partitioning, and sync-ledger
costs that protect the star topology product behavior.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.sync_ledger import SyncLedger


def _ctx(
    device_id: str = "device-0",
    *,
    tenant_id: str = "default",
    owner_user_id: str = "bench",
    persona_id: str = "mochi",
    agent_id: str = "agent-bench",
) -> MemoryActorContext:
    return MemoryActorContext(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        persona_id=persona_id,
        agent_id=agent_id,
        device_id=device_id,
        instance_id=f"{device_id}-runtime",
        session_id="bench-session",
    )


def _record(ctx: MemoryActorContext, idx: int, *, device_id: str, scope: str) -> MemoryWireRecord:
    ext = {"location": {"room": "客厅" if idx % 2 == 0 else "卧室"}}
    return MemoryWireRecord(
        memory_space_id=ctx.memory_space_id,
        key=f"k-{idx}",
        value=f"bench memory {idx} {'乌龙茶' if idx % 7 == 0 else '设备环境'}",
        metadata={
            "memory_space_id": ctx.memory_space_id,
            "scope": scope,
            "visibility": "current_device" if scope == "device" else "all_devices",
            "source_device_id": device_id,
            "target_device_id": device_id if scope == "device" else "",
            "session_id": "bench-session" if idx % 11 == 0 else "",
            "memory_type": "preference" if scope != "device" else "life",
            "extensions": json.dumps(ext, ensure_ascii=False),
            "similarity": 1.0 / (1 + idx % 100),
        },
    )


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0}
    ordered = sorted(values)
    def pct(p: float) -> float:
        pos = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
        return ordered[pos]
    return {
        "p50": pct(50),
        "p95": pct(95),
        "p99": pct(99),
        "mean": statistics.fmean(values),
    }


def bench_recall(records: list[MemoryWireRecord], ctx: MemoryActorContext, *, rounds: int) -> dict:
    registry = RecallPolicyRegistry.default()
    samples: list[float] = []
    leakage = 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        ranked = registry.rank(records, context=ctx, query="客厅 乌龙茶", top_k=8)
        samples.append((time.perf_counter() - t0) * 1000.0)
        leakage += sum(
            1 for rec in ranked
            if rec.metadata.get("scope") == "device"
            and rec.metadata.get("source_device_id") != ctx.device_id
        )
    return {
        "id": "B-01",
        "latency_ms": _percentiles(samples),
        "other_device_leakage": leakage,
    }


def bench_sync(count: int) -> dict:
    with tempfile.TemporaryDirectory() as td:
        ledger = SyncLedger(Path(td) / "sync_ledger.sqlite3")
        samples: list[float] = []
        for idx in range(count):
            t0 = time.perf_counter()
            ledger.mark_synced(
                event_id=f"event-{idx}",
                device_id="device-0",
                instance_id="runtime",
                turn_id=f"turn-{idx}",
                idempotency_hash=f"hash-{idx}",
            )
            samples.append((time.perf_counter() - t0) * 1000.0)
        dup_t0 = time.perf_counter()
        duplicates = sum(
            1 for idx in range(count)
            if ledger.seen(event_id=f"event-{idx}", idempotency_hash=f"hash-{idx}")
        )
        duplicate_ms = (time.perf_counter() - dup_t0) * 1000.0
    return {
        "id": "B-02",
        "events": count,
        "write_latency_ms": _percentiles(samples),
        "duplicates_seen": duplicates,
        "duplicate_scan_ms": duplicate_ms,
    }


def bench_quality(records: list[MemoryWireRecord], ctx: MemoryActorContext) -> dict:
    registry = RecallPolicyRegistry.default()
    ranked = registry.rank(records, context=ctx, query="乌龙茶 客厅", top_k=20)
    persona_relevant = [
        r for r in records
        if r.metadata.get("scope") in {"persona", "global"}
        and "乌龙茶" in str(r.value)
    ]
    persona_hits = [
        r for r in ranked
        if r.metadata.get("scope") in {"persona", "global"}
        and "乌龙茶" in str(r.value)
    ]
    current_device = [
        r for r in ranked
        if r.metadata.get("scope") == "device"
    ]
    correct_device = [
        r for r in current_device
        if r.metadata.get("source_device_id") == ctx.device_id
    ]
    return {
        "id": "B-03",
        "persona_hit_rate": 1.0 if persona_relevant and persona_hits else 0.0,
        "current_device_precision": len(correct_device) / max(1, len(current_device)),
        "other_device_leakage": len(current_device) - len(correct_device),
    }


def bench_extension_overhead(records: list[MemoryWireRecord], ctx: MemoryActorContext) -> dict:
    registry = RecallPolicyRegistry.default()
    no_ext = [
        r.model_copy(update={"metadata": {k: v for k, v in r.metadata.items() if k != "extensions"}})
        for r in records
    ]
    def run(rows: list[MemoryWireRecord]) -> float:
        t0 = time.perf_counter()
        registry.rank(rows, context=ctx, query="客厅", top_k=8)
        return (time.perf_counter() - t0) * 1000.0
    base = [run(no_ext) for _ in range(20)]
    ext = [run(records) for _ in range(20)]
    base_mean = statistics.fmean(base)
    ext_mean = statistics.fmean(ext)
    return {
        "id": "B-04",
        "base_mean_ms": base_mean,
        "extension_mean_ms": ext_mean,
        "overhead_pct": ((ext_mean - base_mean) / base_mean * 100.0) if base_mean else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--devices", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--sync-events", type=int, default=1_000)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--owner-user-id", default="bench")
    parser.add_argument("--persona-id", default="mochi")
    parser.add_argument("--agent-id", default="agent-bench")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    ctx = _ctx(
        "device-0",
        tenant_id=args.tenant_id,
        owner_user_id=args.owner_user_id,
        persona_id=args.persona_id,
        agent_id=args.agent_id,
    )
    records = [
        _record(
            ctx,
            idx,
            device_id=f"device-{idx % max(1, args.devices)}",
            scope="device" if idx % 3 == 0 else "persona",
        )
        for idx in range(args.records)
    ]
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "memory_space_id": ctx.memory_space_id,
        "records": args.records,
        "devices": args.devices,
        "benchmarks": [
            bench_recall(records, ctx, rounds=args.rounds),
            bench_sync(args.sync_events),
            bench_quality(records, ctx),
            bench_extension_overhead(records, ctx),
        ],
    }
    out_dir = Path(args.out_dir) if args.out_dir else Path("reports") / (
        "memory_multi_device_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"report": str(out_dir / "summary.json"), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
