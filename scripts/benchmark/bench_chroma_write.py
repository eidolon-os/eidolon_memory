#!/usr/bin/env python3
"""V10: pure chromadb write throughput bench (bypasses NATS + steward).

Drives ``LockedBackend.ingest_text`` in a tight loop in-process so the measured
latency is ``collection.upsert`` + SQLite commit only — no LLM, no JetStream,
no MCP. Used to compare ``synchronous=NORMAL`` vs ``FULL`` end-to-end commit
cost (the only knob that changes is ``settings.chromadb.synchronous``).

Run two passes (NORMAL then FULL) and compare the JSON outputs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import sqlite3
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eidolon.memory.adapters.locked_backend import LockedBackend  # noqa: E402
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend  # noqa: E402
from eidolon.memory.config.memory_settings import get_memory_settings  # noqa: E402
from eidolon.memory.config.palace_directory import resolve_palace_for_user  # noqa: E402
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized  # noqa: E402

from scripts.benchmark.report import percentiles  # noqa: E402


async def _run(*, count: int, user_id: str, synchronous: str) -> dict:
    settings = get_memory_settings()
    # Mutate in-process to flip the PRAGMA applied by MemPalacePythonBackend.__init__
    settings.chromadb.synchronous = synchronous
    palace = resolve_palace_for_user(settings, user_id)
    ensure_palace_initialized(user_id, palace)

    inner = MemPalacePythonBackend(settings, str(palace))
    backend = LockedBackend(inner)

    # Confirm pragma actually applied
    with sqlite3.connect(str(palace / "chroma.sqlite3"), timeout=5.0) as conn:
        actual_sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    samples_ms: list[float] = []
    for i in range(count):
        text = f"v10 write benchmark sample iteration {i} {uuid.uuid4().hex[:10]}"
        t0 = time.perf_counter()
        await backend.ingest_text(
            wing="Wing_Profile",
            room="profile_core",
            text=text,
            metadata={"v10_bench": True, "user_id": user_id},
        )
        samples_ms.append((time.perf_counter() - t0) * 1000.0)

    stats = percentiles(samples_ms)
    return {
        "synchronous_requested": synchronous,
        "synchronous_actual_code": int(actual_sync),  # 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
        "journal_mode_actual": str(journal_mode),
        "n": count,
        "user_id": user_id,
        "palace": str(palace),
        "writes_per_second": round(count / (sum(samples_ms) / 1000.0), 1),
        "latency_ms": stats,
    }


def _pretty(row: dict) -> str:
    lat = row["latency_ms"]
    return (
        f"  synchronous={row['synchronous_requested']} (code={row['synchronous_actual_code']}, "
        f"journal={row['journal_mode_actual']})\n"
        f"  n={row['n']}  throughput={row['writes_per_second']} writes/s\n"
        f"  P50={lat['p50']:.2f}ms  P95={lat['p95']:.2f}ms  "
        f"P99={lat['p99']:.2f}ms  max={lat['max']:.2f}ms  "
        f"mean={lat['mean']:.2f}ms"
    )


def _raw_sqlite_bench(palace_path: Path, *, count: int, synchronous: str) -> dict:
    """Pure SQLite write throughput on chroma.sqlite3 (no chromadb in the loop).

    Inserts into a private bench table so it doesn't collide with chromadb
    schema. Runs WAL + the chosen ``synchronous`` mode on the connection
    actually doing the work, then drops the table.
    """
    p = palace_path / "chroma.sqlite3"
    if not p.is_file():
        raise FileNotFoundError(p)
    conn = sqlite3.connect(str(p), timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={synchronous}")
        conn.execute("PRAGMA busy_timeout=5000")
        applied = conn.execute("PRAGMA synchronous").fetchone()[0]
        conn.execute(
            "CREATE TABLE IF NOT EXISTS v10_raw_bench(id INTEGER PRIMARY KEY, payload TEXT)"
        )
        conn.execute("DELETE FROM v10_raw_bench")
        conn.commit()

        samples_ms: list[float] = []
        for i in range(count):
            payload = f"raw write {i} {uuid.uuid4().hex}"
            t0 = time.perf_counter()
            conn.execute("INSERT INTO v10_raw_bench(payload) VALUES (?)", (payload,))
            conn.commit()
            samples_ms.append((time.perf_counter() - t0) * 1000.0)

        conn.execute("DROP TABLE v10_raw_bench")
        conn.commit()
    finally:
        conn.close()

    stats = percentiles(samples_ms)
    return {
        "synchronous_requested": synchronous,
        "synchronous_actual_code": int(applied),
        "n": count,
        "writes_per_second": round(count / (sum(samples_ms) / 1000.0), 1),
        "latency_ms": stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--user-id", default="bench-v10")
    parser.add_argument(
        "--synchronous",
        choices=["NORMAL", "FULL"],
        action="append",
        default=None,
        help="PRAGMA synchronous mode to bench; pass twice to compare. Default: both.",
    )
    parser.add_argument(
        "--raw-sqlite",
        action="store_true",
        help="Also run a pure-SQLite write bench on chroma.sqlite3 with the "
        "synchronous PRAGMA actually applied to the writer connection.",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    modes = args.synchronous or ["NORMAL", "FULL"]
    results = []
    for mode in modes:
        print(f"[V10] running with synchronous={mode} (n={args.count})…")
        row = asyncio.run(_run(count=args.count, user_id=args.user_id, synchronous=mode))
        results.append(row)
        print(_pretty(row))

    summary = {"runs": results}
    if len(results) == 2:
        a, b = results[0], results[1]
        ratio = (
            b["latency_ms"]["p50"] / max(0.001, a["latency_ms"]["p50"])
            if a["latency_ms"]["p50"] > 0
            else 0
        )
        tput_ratio = (
            a["writes_per_second"] / max(0.001, b["writes_per_second"])
            if b["writes_per_second"] > 0
            else 0
        )
        summary["comparison"] = {
            "p50_ratio_b_over_a": round(ratio, 2),
            "throughput_ratio_a_over_b": round(tput_ratio, 2),
            "note": (
                f"{b['synchronous_requested']} latency P50 is "
                f"{ratio:.2f}× {a['synchronous_requested']}'s; throughput "
                f"{tput_ratio:.2f}× lower"
            ),
        }
        print(f"\n[V10] {summary['comparison']['note']}")

    if args.raw_sqlite:
        from eidolon.memory.config.memory_settings import get_memory_settings
        from eidolon.memory.config.palace_directory import resolve_palace_for_user

        settings = get_memory_settings()
        palace = resolve_palace_for_user(settings, args.user_id)
        raw_results = []
        for mode in modes:
            print(f"\n[V10 raw-sqlite] synchronous={mode} (n={args.count})…")
            row = _raw_sqlite_bench(palace, count=args.count, synchronous=mode)
            raw_results.append(row)
            lat = row["latency_ms"]
            print(
                f"  synchronous={row['synchronous_requested']} "
                f"(code={row['synchronous_actual_code']})\n"
                f"  n={row['n']}  throughput={row['writes_per_second']} writes/s\n"
                f"  P50={lat['p50']:.2f}ms  P95={lat['p95']:.2f}ms  "
                f"P99={lat['p99']:.2f}ms  max={lat['max']:.2f}ms  "
                f"mean={lat['mean']:.2f}ms"
            )
        summary["raw_sqlite_runs"] = raw_results
        if len(raw_results) == 2:
            a, b = raw_results
            ratio = b["latency_ms"]["p50"] / max(0.001, a["latency_ms"]["p50"])
            tput = a["writes_per_second"] / max(0.001, b["writes_per_second"])
            summary["raw_sqlite_comparison"] = {
                "p50_ratio_b_over_a": round(ratio, 2),
                "throughput_ratio_a_over_b": round(tput, 2),
                "note": (
                    f"RAW SQLITE: {b['synchronous_requested']} P50 is "
                    f"{ratio:.2f}× {a['synchronous_requested']}'s; throughput "
                    f"{tput:.2f}× lower"
                ),
            }
            print(f"\n[V10 raw-sqlite] {summary['raw_sqlite_comparison']['note']}")

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[V10] wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
