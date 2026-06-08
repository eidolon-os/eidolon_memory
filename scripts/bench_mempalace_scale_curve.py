from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

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

CATEGORIES = [
    (
        "backend_performance",
        "Qdrant Chroma vector backend performance latency throughput benchmark",
    ),
    (
        "memory_quality",
        "stable accurate memory recall quality preference fact retrieval",
    ),
    (
        "home_lan",
        "desktop family local network home LAN deployment",
    ),
    (
        "voice_fast_path",
        "voice fast path shared embedding low latency recall",
    ),
    (
        "admin_snapshot",
        "admin memory hierarchy snapshot palace graph browse",
    ),
    (
        "sqlite_contention",
        "SQLite lock contention disk IO WAL checkpoint storage stability",
    ),
    (
        "kg_fusion",
        "knowledge graph entity relationship temporal fusion",
    ),
    (
        "chinese_profile",
        "中文 用户 偏好 乌龙茶 家庭 局域网 记忆",
    ),
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
                "exclude_current_session": False,
                "voice_wings": WINGS,
                "theme_top_k": 0,
            },
        }
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[idx]


def _summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "mean": statistics.fmean(values) if values else 0.0,
    }


async def _timed(
    values: list[float],
    errors: list[str],
    label: str,
    op: Callable[[], Awaitable[Any]],
) -> Any:
    t0 = time.perf_counter()
    try:
        result = await op()
    except Exception as exc:  # noqa: BLE001 - benchmark records backend failures
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return None
    values.append((time.perf_counter() - t0) * 1000)
    return result


def _doc(idx: int) -> tuple[str, str, str, dict[str, Any]]:
    category, phrase = CATEGORIES[idx % len(CATEGORIES)]
    wing = WINGS[idx % len(WINGS)]
    room = f"scale_{category}"
    marker = f"scale_marker_{idx:06d}"
    # Repeated category phrase gives semantic search a meaningful signal while
    # the marker keeps records auditable in admin/listing paths.
    text = (
        f"{marker}. Category {category}. {phrase}. "
        f"Default user scale benchmark memory record {idx}. "
        f"This synthetic drawer tests retrieval behavior as palace size grows."
    )
    metadata = {
        "user_id": "default",
        "wing": wing,
        "room": room,
        "source_file": f"scale/default/{category}/{idx}.txt",
        "category": category,
        "marker": marker,
        "added_by": "scale-benchmark",
    }
    return wing, room, text, metadata


def _ids(result: Any) -> list[str]:
    if isinstance(result, dict):
        return list(result.get("ids") or [])
    return list(getattr(result, "ids", None) or [])


def _metadatas(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        metas = list(result.get("metadatas") or [])
    else:
        metas = list(getattr(result, "metadatas", None) or [])
    if metas and isinstance(metas[0], list):
        return [m for group in metas for m in group]
    return metas


def _collection_count(col: Any) -> int:
    if hasattr(col, "count"):
        return int(col.count())
    if hasattr(col, "estimated_count"):
        return int(col.estimated_count())
    return 0


def _upsert_batch_sync(palace: str, backend: str, start: int, end: int, batch_size: int) -> int:
    from mempalace.palace import get_collection

    col = get_collection(palace, create=True, backend=backend)
    written = 0
    for lo in range(start, end, batch_size):
        hi = min(end, lo + batch_size)
        ids: list[str] = []
        docs: list[str] = []
        metas: list[dict[str, Any]] = []
        for idx in range(lo, hi):
            _wing, _room, text, metadata = _doc(idx)
            ids.append(f"scale_drawer_{idx:08d}")
            docs.append(text)
            metas.append(metadata)
        col.upsert(ids=ids, documents=docs, metadatas=metas)
        written += len(ids)
    return written


async def _seed_to_target(
    *,
    palace: str,
    backend: str,
    current_target: int,
    next_target: int,
    batch_size: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    written = await asyncio.to_thread(
        _upsert_batch_sync,
        palace,
        backend,
        current_target,
        next_target,
        batch_size,
    )
    return {
        "from": current_target,
        "to": next_target,
        "written": written,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "docs_per_sec": written / max(0.001, time.perf_counter() - started),
    }


def _query_for(idx: int) -> tuple[str, str]:
    category, phrase = CATEGORIES[idx % len(CATEGORIES)]
    return category, f"{phrase} default memory benchmark"


def _category_precision(records: list[Any], category: str) -> float:
    if not records:
        return 0.0
    hits = 0
    for rec in records:
        meta = getattr(rec, "metadata", {}) or {}
        value = str(getattr(rec, "value", "") or "")
        if meta.get("category") == category or f"Category {category}" in value:
            hits += 1
    return hits / len(records)


def _category_precision_from_raw(result: Any, category: str) -> float:
    metas = _metadatas(result)
    if not metas:
        return 0.0
    return sum(1 for meta in metas if (meta or {}).get("category") == category) / len(metas)


async def _measure_point(
    *,
    backend: LockedBackend,
    settings: MemorySettings,
    palace: str,
    backend_name: str,
    size: int,
    top_k: int,
    queries: int,
    write_samples: int,
    concurrency: int,
) -> dict[str, Any]:
    errors: list[str] = []
    normal_lat: list[float] = []
    voice_lat: list[float] = []
    backend_search_lat: list[float] = []
    direct_query_lat: list[float] = []
    get_all_lat: list[float] = []
    get_by_id_lat: list[float] = []
    write_lat: list[float] = []
    normal_precision: list[float] = []
    voice_precision: list[float] = []
    backend_precision: list[float] = []
    direct_precision: list[float] = []

    from mempalace.palace import get_collection

    col = get_collection(palace, create=False, backend=backend_name)
    count = await asyncio.to_thread(_collection_count, col)

    sample_indices = [
        int(round(i * max(0, size - 1) / max(1, queries - 1)))
        for i in range(queries)
    ]
    for idx in sample_indices:
        category, query = _query_for(idx)
        normal = await _timed(
            normal_lat,
            errors,
            f"normal:{size}:{idx}",
            lambda query=query: search_all_wings_mcp_style(
                backend,
                settings,
                query=query,
                user_id="default",
                top_k=top_k,
                wing=None,
                room=None,
                for_voice=False,
                palace_path=palace,
            ),
        )
        normal_precision.append(_category_precision(normal or [], category))

        voice = await _timed(
            voice_lat,
            errors,
            f"voice:{size}:{idx}",
            lambda query=query: search_all_wings_mcp_style(
                backend,
                settings,
                query=query,
                user_id="default",
                top_k=top_k,
                wing=None,
                room=None,
                for_voice=True,
                palace_path=palace,
            ),
        )
        voice_precision.append(_category_precision(voice or [], category))

        wing, _room, _text, _meta = _doc(idx)
        hits = await _timed(
            backend_search_lat,
            errors,
            f"backend_search:{size}:{idx}",
            lambda query=query, wing=wing: backend.search(
                query,
                wing=wing,
                n_results=top_k,
                room=None,
            ),
        )
        backend_precision.append(_category_precision(hits or [], category))

        async def direct_query(query=query, category=category) -> Any:
            del category
            return await asyncio.to_thread(
                col.query,
                query_texts=[query],
                n_results=top_k,
                include=["metadatas", "documents", "distances"],
            )

        raw = await _timed(direct_query_lat, errors, f"direct_query:{size}:{idx}", direct_query)
        direct_precision.append(_category_precision_from_raw(raw, category))

    for offset in [0, max(0, size // 2), max(0, size - 25)]:
        await _timed(
            get_all_lat,
            errors,
            f"get_all:{size}:{offset}",
            lambda offset=offset: backend.get_all("default", limit=25, offset=offset),
        )
    for idx in sample_indices[: min(8, len(sample_indices))]:
        await _timed(
            get_by_id_lat,
            errors,
            f"get:{size}:{idx}",
            lambda idx=idx: backend.get("default", f"scale_drawer_{idx:08d}"),
        )

    write_start = size + 1_000_000
    for idx in range(write_start, write_start + write_samples):
        wing, room, text, meta = _doc(idx)
        await _timed(
            write_lat,
            errors,
            f"sample_write:{size}:{idx}",
            lambda wing=wing, room=room, text=text, meta=meta: backend.ingest_text(
                wing=wing,
                room=room,
                text=text,
                metadata=meta,
            ),
        )

    mixed_errors: list[str] = []
    mixed_lat: list[float] = []
    sem = asyncio.Semaphore(concurrency)

    async def mixed_one(n: int) -> None:
        idx = sample_indices[n % len(sample_indices)]
        category, query = _query_for(idx)
        del category
        async with sem:
            await _timed(
                mixed_lat,
                mixed_errors,
                f"mixed:{size}:{n}",
                lambda query=query: search_all_wings_mcp_style(
                    backend,
                    settings,
                    query=query,
                    user_id="default",
                    top_k=top_k,
                    wing=None,
                    room=None,
                    for_voice=bool(n % 2),
                    palace_path=palace,
                ),
            )

    await asyncio.gather(*(mixed_one(n) for n in range(concurrency * 2)))

    return {
        "target_size": size,
        "observed_count": count,
        "errors": errors[:20],
        "error_count": len(errors),
        "normal_ms": _summary(normal_lat),
        "voice_ms": _summary(voice_lat),
        "backend_search_ms": _summary(backend_search_lat),
        "direct_query_ms": _summary(direct_query_lat),
        "get_all_page_ms": _summary(get_all_lat),
        "get_by_id_ms": _summary(get_by_id_lat),
        "sample_write_ms": _summary(write_lat),
        "mixed_recall_ms": _summary(mixed_lat),
        "mixed_error_count": len(mixed_errors),
        "precision": {
            "normal_mean": statistics.fmean(normal_precision) if normal_precision else 0.0,
            "voice_mean": statistics.fmean(voice_precision) if voice_precision else 0.0,
            "backend_search_mean": statistics.fmean(backend_precision) if backend_precision else 0.0,
            "direct_query_mean": statistics.fmean(direct_precision) if direct_precision else 0.0,
        },
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = _settings(args)
    apply_mempalace_backend_env(settings)
    backend_name = selected_mempalace_backend(settings)
    palace = Path(args.palace).expanduser().resolve()
    ensure_palace_initialized(
        "default",
        palace,
        backend=backend_name,
        env=mempalace_backend_env(settings),
    )
    backend = LockedBackend(MemPalacePythonBackend(settings, str(palace)))

    sizes = [int(part) for part in args.sizes.split(",") if part.strip()]
    sizes = sorted(set(sizes))
    current_target = 0
    points = []
    total_started = time.perf_counter()
    for size in sizes:
        seed = {"from": current_target, "to": size, "written": 0, "elapsed_ms": 0.0, "docs_per_sec": 0.0}
        if size > current_target:
            seed = await _seed_to_target(
                palace=str(palace),
                backend=backend_name,
                current_target=current_target,
                next_target=size,
                batch_size=args.seed_batch_size,
            )
            current_target = size
        measured = await _measure_point(
            backend=backend,
            settings=settings,
            palace=str(palace),
            backend_name=backend_name,
            size=size,
            top_k=args.top_k,
            queries=args.queries,
            write_samples=args.write_samples,
            concurrency=args.concurrency,
        )
        points.append({"seed": seed, "measure": measured})
    return {
        "backend": backend_name,
        "palace": str(palace),
        "sizes": sizes,
        "user_id": "default",
        "seed_batch_size": args.seed_batch_size,
        "queries_per_point": args.queries,
        "total_elapsed_ms": (time.perf_counter() - total_started) * 1000,
        "points": points,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scale curve benchmark for default-user MemPalace backend.")
    parser.add_argument("--backend", choices=["chroma", "qdrant"], required=True)
    parser.add_argument("--palace", required=True)
    parser.add_argument("--sizes", default="1000,5000,10000,30000,50000,100000")
    parser.add_argument("--seed-batch-size", type=int, default=256)
    parser.add_argument("--queries", type=int, default=16)
    parser.add_argument("--write-samples", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--qdrant-namespace", default="eidolon-scale")
    parser.add_argument("--qdrant-timeout", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(asyncio.run(_run(parse_args())), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
