from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

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

SEED_ROWS = [
    ("Wing_Profile", "tea", "The user prefers oolong tea and avoids coffee."),
    ("Wing_Profile", "schedule", "The user works best after 10 AM."),
    ("Wing_Work", "backend", "Qdrant is being evaluated as the memory vector backend."),
    ("Wing_Work", "mempalace", "MemPalace currently defaults to Chroma embedded storage."),
    ("Wing_Work", "locking", "The agent runner serializes memory access with one asyncio lock."),
    ("Wing_Emotion", "stability", "The user values memory that is fast, stable, and accurate."),
    ("Wing_Relationship", "family", "The home deployment is expected to serve a few family users."),
    ("Wing_Future", "roadmap", "The next backend experiment compares Chroma and Qdrant."),
    ("Wing_Life", "home", "The service is intended for desktop or local network use."),
    ("Wing_Event", "incident", "A transient disk I/O error appeared during memory reads."),
]

QUERIES = [
    "Which vector backend are we evaluating?",
    "What does the user care about for memory quality?",
    "What storage backend does MemPalace use by default?",
    "How does the agent runner control memory concurrency?",
    "What deployment environment is expected?",
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
            },
        }
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[idx]


def _keys(records: list[Any]) -> list[str]:
    return [str(r.key) for r in records]


async def _seed(backend: LockedBackend) -> None:
    for wing, room, text in SEED_ROWS:
        await backend.ingest_text(
            wing=wing,
            room=room,
            text=text,
            metadata={"user_id": "bench", "source_file": f"seed/{wing}/{room}.txt"},
        )


async def _run_once(
    backend: LockedBackend,
    settings: MemorySettings,
    *,
    query: str,
    palace_path: str,
    top_k: int,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    normal = await search_all_wings_mcp_style(
        backend,
        settings,
        query=query,
        user_id="bench",
        top_k=top_k,
        wing=None,
        room=None,
        for_voice=False,
        palace_path=palace_path,
    )
    normal_ms = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    voice = await search_all_wings_mcp_style(
        backend,
        settings,
        query=query,
        user_id="bench",
        top_k=top_k,
        wing=None,
        room=None,
        for_voice=True,
        palace_path=palace_path,
    )
    voice_ms = (time.perf_counter() - t1) * 1000

    normal_keys = set(_keys(normal))
    voice_keys = set(_keys(voice))
    union = normal_keys | voice_keys
    overlap = (len(normal_keys & voice_keys) / len(union)) if union else 1.0
    return {
        "normal_ms": normal_ms,
        "voice_ms": voice_ms,
        "normal_keys": sorted(normal_keys),
        "voice_keys": sorted(voice_keys),
        "overlap": overlap,
    }


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
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
    backend = LockedBackend(MemPalacePythonBackend(settings, str(palace)))
    await _seed(backend)

    normal_latencies: list[float] = []
    voice_latencies: list[float] = []
    overlaps: list[float] = []
    errors: list[str] = []
    samples: list[dict[str, Any]] = []

    for idx in range(args.iterations):
        query = QUERIES[idx % len(QUERIES)]
        try:
            sample = await _run_once(
                backend,
                settings,
                query=query,
                palace_path=str(palace),
                top_k=args.top_k,
            )
        except Exception as exc:  # noqa: BLE001 - benchmark records backend failures
            errors.append(f"{query}: {type(exc).__name__}: {exc}")
            continue
        normal_latencies.append(sample["normal_ms"])
        voice_latencies.append(sample["voice_ms"])
        overlaps.append(sample["overlap"])
        if len(samples) < 5:
            samples.append({"query": query, **sample})

    return {
        "backend": backend_name,
        "palace": str(palace),
        "iterations": args.iterations,
        "completed": len(normal_latencies),
        "errors": errors,
        "normal_ms": {
            "p50": _percentile(normal_latencies, 0.50),
            "p95": _percentile(normal_latencies, 0.95),
            "mean": statistics.fmean(normal_latencies) if normal_latencies else 0.0,
        },
        "voice_ms": {
            "p50": _percentile(voice_latencies, 0.50),
            "p95": _percentile(voice_latencies, 0.95),
            "mean": statistics.fmean(voice_latencies) if voice_latencies else 0.0,
        },
        "topk_overlap": {
            "mean": statistics.fmean(overlaps) if overlaps else 0.0,
            "min": min(overlaps) if overlaps else 0.0,
        },
        "samples": samples,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare MemPalace backend recall behavior.")
    parser.add_argument("--backend", choices=["chroma", "qdrant"], required=True)
    parser.add_argument("--palace", required=True)
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--qdrant-namespace", default="eidolon-ab")
    parser.add_argument("--qdrant-timeout", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    result = asyncio.run(main_async(parse_args()))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
