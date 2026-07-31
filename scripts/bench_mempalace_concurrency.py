from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.public_recall import search_all_wings_mcp_style
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.mempalace_backend import (
    apply_mempalace_backend_env,
    mempalace_backend_env,
    selected_mempalace_backend,
)
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized

WINGS = [
    "Wing_Profile",
    "Wing_Work",
    "Wing_Emotion",
    "Wing_Relationship",
    "Wing_Future",
    "Wing_Life",
    "Wing_Event",
]

QUERIES = [
    "Which backend is being evaluated?",
    "What does the user value for memory?",
    "How is memory access serialized?",
    "What deployment environment is expected?",
    "What was the transient failure class?",
]


def _settings(args: argparse.Namespace) -> MemorySettings:
    return MemorySettings.model_validate(
        {
            "mempalace": {
                "backend": args.backend,
                "qdrant_url": args.qdrant_url,
                "qdrant_namespace": args.qdrant_namespace,
                "qdrant_timeout_seconds": args.qdrant_timeout,
            },
            "recall": {
                "top_k": args.top_k,
                "exclude_recent_minutes": 0,
                "voice_wings": WINGS,
            },
        }
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[idx]


async def _seed(backend: Any, count: int) -> None:
    for idx in range(count):
        wing = WINGS[idx % len(WINGS)]
        await backend.ingest_text(
            wing=wing,
            room=f"seed_{idx % 5}",
            text=(
                f"Concurrency seed {idx}: Eidolon memory should stay fast, stable, "
                f"and accurate while evaluating {wing}."
            ),
            metadata={
                "memory_space_id": "bench",
                "memory_realm_id": "bench",
                "user_id": "bench",
                "wing": wing,
                "privacy": "normal",
                "scope": "global",
                "visibility": "all_devices",
                "source_file": f"seed/concurrency/{idx}.txt",
            },
        )


async def _run_search(
    backend: Any,
    settings: MemorySettings,
    *,
    palace_path: str,
    query: str,
    for_voice: bool,
    top_k: int,
) -> int:
    rows = await search_all_wings_mcp_style(
        backend,
        settings,
        query=query,
        context=MemoryActorContext(
            memory_realm_id="bench",
            memory_space_id="bench",
        ),
        top_k=top_k,
        wing=None,
        room=None,
        for_voice=for_voice,
        palace_path=palace_path,
    )
    return len(rows)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = _settings(args)
    apply_mempalace_backend_env(settings)
    backend_name = selected_mempalace_backend(settings)
    palace = Path(args.palace).expanduser().resolve()
    ensure_palace_initialized(
        "bench",
        palace,
        backend=backend_name,
        env=mempalace_backend_env(settings),
    )

    inner = MemPalacePythonBackend(settings, str(palace), memory_space_id="bench")
    backend: Any = inner if args.raw else LockedBackend(inner)
    await _seed(backend, args.seed)

    sem = asyncio.Semaphore(args.concurrency)
    latencies: dict[str, list[float]] = {"normal": [], "voice": [], "write": []}
    empty_reads = 0
    errors: list[str] = []

    async def one(idx: int) -> None:
        nonlocal empty_reads
        kind = "write" if idx % args.write_every == 0 else ("voice" if idx % 2 else "normal")
        async with sem:
            started = time.perf_counter()
            try:
                if kind == "write":
                    await backend.ingest_text(
                        wing=WINGS[idx % len(WINGS)],
                        room=f"live_{idx % 7}",
                        text=(
                            f"Concurrent write {idx}: backend {backend_name} "
                            "mixed read write test."
                        ),
                        metadata={
                            "memory_space_id": "bench",
                            "memory_realm_id": "bench",
                            "user_id": "bench",
                            "wing": WINGS[idx % len(WINGS)],
                            "privacy": "normal",
                            "scope": "global",
                            "visibility": "all_devices",
                            "source_file": f"stress/concurrency/{idx}.txt",
                        },
                    )
                else:
                    count = await _run_search(
                        backend,
                        settings,
                        palace_path=str(palace),
                        query=QUERIES[idx % len(QUERIES)],
                        for_voice=(kind == "voice"),
                        top_k=args.top_k,
                    )
                    if count == 0:
                        empty_reads += 1
                latencies[kind].append((time.perf_counter() - started) * 1000)
            except Exception as exc:  # noqa: BLE001 - stress test records all backend failures
                errors.append(f"{kind}#{idx}: {type(exc).__name__}: {exc}")

    started = time.perf_counter()
    await asyncio.gather(*(one(idx) for idx in range(args.operations)))
    elapsed_ms = (time.perf_counter() - started) * 1000

    return {
        "backend": backend_name,
        "mode": "raw" if args.raw else "locked",
        "palace": str(palace),
        "operations": args.operations,
        "concurrency": args.concurrency,
        "write_every": args.write_every,
        "completed": sum(len(v) for v in latencies.values()),
        "errors": errors[:20],
        "error_count": len(errors),
        "empty_reads": empty_reads,
        "elapsed_ms": elapsed_ms,
        "latency_ms": {
            key: {
                "count": len(values),
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "mean": statistics.fmean(values) if values else 0.0,
            }
            for key, values in latencies.items()
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stress MemPalace backend read/write concurrency.")
    parser.add_argument("--backend", choices=["chroma", "qdrant"], required=True)
    parser.add_argument("--palace", required=True)
    parser.add_argument("--operations", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--write-every", type=int, default=5)
    parser.add_argument("--seed", type=int, default=40)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--raw", action="store_true", help="Bypass LockedBackend.")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--qdrant-namespace", default="eidolon-concurrency")
    parser.add_argument("--qdrant-timeout", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(asyncio.run(_run(parse_args())), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
