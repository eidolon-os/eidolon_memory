#!/usr/bin/env python3
"""Measure Chroma/MemPalace client reload latency (Phase 0 gate)."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path


def _percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(samples)
    n = len(ordered)

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, int(p * n) - 1))
        return ordered[idx]

    return {
        "p50": pct(0.50),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": ordered[-1],
    }


def bench_close_reopen(palace: str, *, rounds: int) -> dict[str, float]:
    from mempalace.backends.chroma import ChromaBackend
    from mempalace.palace import _DEFAULT_BACKEND
    from mempalace.searcher import search_memories

    if not isinstance(_DEFAULT_BACKEND, ChromaBackend):
        return {"error": 1.0}

    samples: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        _DEFAULT_BACKEND.close_palace(palace)
        _DEFAULT_BACKEND._clients.pop(palace, None)
        _DEFAULT_BACKEND._freshness.pop(palace, None)
        search_memories("warmup", palace_path=palace, wing="Wing_Profile", n_results=1)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return _percentiles(samples)


def bench_pop_cache(palace: str, *, rounds: int) -> dict[str, float]:
    from mempalace.backends.chroma import ChromaBackend
    from mempalace.palace import _DEFAULT_BACKEND
    from mempalace.searcher import search_memories

    if not isinstance(_DEFAULT_BACKEND, ChromaBackend):
        return {"error": 1.0}

    samples: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        _DEFAULT_BACKEND._clients.pop(palace, None)
        _DEFAULT_BACKEND._freshness.pop(palace, None)
        search_memories("warmup", palace_path=palace, wing="Wing_Profile", n_results=1)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return _percentiles(samples)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palace", required=True, help="Absolute palace path (required in D1)")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument(
        "--out",
        default="reports/poc_chroma_reload.json",
        help="JSON output path",
    )
    args = parser.parse_args()
    palace = args.palace
    # NOTE: this Phase-0 PoC is obsolete in D1 (single-process palace, no
    # double-buffer); kept as historical baseline.

    close_stats = bench_close_reopen(palace, rounds=args.rounds)
    pop_stats = bench_pop_cache(palace, rounds=args.rounds)

    report = {
        "palace": palace,
        "rounds": args.rounds,
        "close_reopen_ms": close_stats,
        "pop_cache_ms": pop_stats,
        "gate_ms": 600,
        "pass": close_stats.get("p95", 9999) > 600,
        "recommendation": (
            "use_double_buffer"
            if close_stats.get("p95", 0) > 600
            else "pop_cache_may_suffice"
        ),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] or report["recommendation"] == "use_double_buffer" else 0


if __name__ == "__main__":
    raise SystemExit(main())
