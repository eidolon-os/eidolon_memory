from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import (
    MemPalacePythonBackend,
    _drawer_id,
)
from eidolon.memory.application.forget import find_forget_candidates
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

MIXED_LOAD_MODEL = "closed_loop_bounded_outstanding"


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
                "theme_top_k": 0,
            },
            "runtime": {
                "read": {
                    "normal_shared_query_embedding": args.normal_shared_embedding,
                }
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
        "p99": _percentile(values, 0.99),
        "mean": statistics.fmean(values) if values else 0.0,
    }


def _parse_concurrencies(value: str) -> list[int]:
    values = sorted({int(part.strip()) for part in value.split(",") if part.strip()})
    if not values:
        raise ValueError("concurrencies must contain at least one value")
    if values[0] <= 0:
        raise ValueError("concurrencies must be positive")
    return values


def _max_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


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
        "memory_space_id": "default",
        "wing": wing,
        "room": room,
        "source_file": f"scale/default/{category}/{idx}.txt",
        "category": category,
        "marker": marker,
        "added_by": "scale-benchmark",
        # Keep a small but growing restricted population so the benchmark
        # catches privacy filtering that accidentally scales with all archived
        # drawers instead of only the current top-k hits.
        "privacy": "do_not_recall" if idx % 50 == 0 else "normal",
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
            ids.append(_drawer_id(_wing, _room, text))
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
    backend: Any,
    settings: MemorySettings,
    palace: str,
    backend_name: str,
    size: int,
    top_k: int,
    queries: int,
    write_samples: int,
    concurrencies: list[int],
    mixed_operations: int,
    privacy_page_size: int,
) -> dict[str, Any]:
    errors: list[str] = []
    normal_lat: list[float] = []
    voice_lat: list[float] = []
    backend_search_lat: list[float] = []
    direct_query_lat: list[float] = []
    get_all_lat: list[float] = []
    get_by_id_lat: list[float] = []
    write_lat: list[float] = []
    privacy_scan_lat: list[float] = []
    normal_precision: list[float] = []
    voice_precision: list[float] = []
    backend_precision: list[float] = []
    direct_precision: list[float] = []
    privacy_leak_count = 0
    context = MemoryActorContext(memory_realm_id="default", memory_space_id="default")

    from mempalace.palace import get_collection

    col = get_collection(palace, create=False, backend=backend_name)
    count = await asyncio.to_thread(_collection_count, col)

    sample_indices = [
        int(round(i * max(0, size - 1) / max(1, queries - 1))) for i in range(queries)
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
                context=context,
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
                context=context,
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
        privacy_leak_count += sum(
            1
            for hit in hits or []
            if str((getattr(hit, "metadata", {}) or {}).get("privacy", "")).lower()
            in {"private", "do_not_recall"}
        )

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
        wing, room, text, _meta = _doc(idx)
        await _timed(
            get_by_id_lat,
            errors,
            f"get:{size}:{idx}",
            lambda wing=wing, room=room, text=text: backend.get(
                "default", _drawer_id(wing, room, text)
            ),
        )

    deep_marker = f"scale_marker_{max(0, size - 1):06d}"
    privacy_candidates = await _timed(
        privacy_scan_lat,
        errors,
        f"privacy_scan:{size}",
        lambda: find_forget_candidates(
            backend,
            "default",
            deep_marker,
            max_scan=max(1, size + 100),
            max_candidates=5,
            page_size=privacy_page_size,
        ),
    )

    # A resumed benchmark must still measure a real insert instead of hitting
    # deterministic idempotency from an earlier run at the same size.
    write_start = size + 1_000_000 + (time.time_ns() % 1_000_000_000)
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

    mixed_curve: dict[str, Any] = {}
    for concurrency in concurrencies:
        mixed_errors: list[str] = []
        mixed_lat: list[float] = []
        sem = asyncio.Semaphore(concurrency)

        async def mixed_one(n: int) -> None:
            idx = sample_indices[n % len(sample_indices)]
            category, query = _query_for(idx)
            del category
            # Standard closed-loop load: the semaphore limits the number of
            # outstanding callers. Load-generator queue time is excluded;
            # waiting on the production Realm/backend lock is included.
            async with sem:
                await _timed(
                    mixed_lat,
                    mixed_errors,
                    f"mixed:{size}:c{concurrency}:{n}",
                    lambda query=query: search_all_wings_mcp_style(
                        backend,
                        settings,
                        query=query,
                        context=context,
                        top_k=top_k,
                        wing=None,
                        room=None,
                        for_voice=bool(n % 2),
                        palace_path=palace,
                    ),
                )

        await asyncio.gather(*(mixed_one(n) for n in range(mixed_operations)))
        mixed_curve[str(concurrency)] = {
            "operations": mixed_operations,
            "latency_ms": _summary(mixed_lat),
            "error_count": len(mixed_errors),
            "errors": mixed_errors[:20],
        }

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
        "privacy_deep_scan_ms": _summary(privacy_scan_lat),
        "privacy_deep_candidate_count": len(privacy_candidates or []),
        "mixed_recall_by_concurrency": mixed_curve,
        "privacy_leak_count": privacy_leak_count,
        "process_max_rss_bytes": _max_rss_bytes(),
        "palace_bytes": sum(
            path.stat().st_size for path in Path(palace).rglob("*") if path.is_file()
        ),
        "precision": {
            "normal_mean": statistics.fmean(normal_precision) if normal_precision else 0.0,
            "voice_mean": statistics.fmean(voice_precision) if voice_precision else 0.0,
            "backend_search_mean": (
                statistics.fmean(backend_precision) if backend_precision else 0.0
            ),
            "direct_query_mean": statistics.fmean(direct_precision) if direct_precision else 0.0,
        },
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.initial_size < 0:
        raise ValueError("initial_size must be non-negative")
    if args.mixed_operations < 1:
        raise ValueError("mixed_operations must be positive")
    if args.privacy_page_size < 1:
        raise ValueError("privacy_page_size must be positive")
    sizes = sorted({int(part) for part in args.sizes.split(",") if part.strip()})
    if not sizes:
        raise ValueError("sizes must contain at least one value")
    if args.raw and (args.write_samples != 0 or any(size > args.initial_size for size in sizes)):
        raise ValueError("raw mode is read-only: use --write-samples 0 on an existing size")
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
    inner = MemPalacePythonBackend(settings, str(palace), memory_space_id="default")
    backend: Any = inner if args.raw else LockedBackend(inner)

    current_target = args.initial_size
    points = []
    total_started = time.perf_counter()
    for size in sizes:
        seed = {
            "from": current_target,
            "to": size,
            "written": 0,
            "elapsed_ms": 0.0,
            "docs_per_sec": 0.0,
        }
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
            concurrencies=_parse_concurrencies(args.concurrencies),
            mixed_operations=args.mixed_operations,
            privacy_page_size=args.privacy_page_size,
        )
        points.append({"seed": seed, "measure": measured})
    return {
        "backend": backend_name,
        "mode": "raw" if args.raw else "locked",
        "normal_shared_query_embedding": args.normal_shared_embedding,
        "palace": str(palace),
        "sizes": sizes,
        "estimated_years_at_records_per_day": {
            str(size): round(size / max(1, args.long_term_records_per_day) / 365, 2)
            for size in sizes
        },
        "user_id": "default",
        "seed_batch_size": args.seed_batch_size,
        "initial_size": args.initial_size,
        "queries_per_point": args.queries,
        "mixed_operations_per_concurrency": args.mixed_operations,
        "privacy_page_size": args.privacy_page_size,
        "concurrencies": _parse_concurrencies(args.concurrencies),
        "mixed_load_model": MIXED_LOAD_MODEL,
        "total_elapsed_ms": (time.perf_counter() - total_started) * 1000,
        "points": points,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scale curve benchmark for default-user MemPalace backend."
    )
    parser.add_argument("--backend", choices=["chroma", "qdrant"], required=True)
    parser.add_argument("--palace", required=True)
    parser.add_argument("--sizes", default="1000,5000,10000,30000,50000,100000")
    parser.add_argument(
        "--initial-size",
        type=int,
        default=0,
        help="Known production-shaped rows already seeded in this isolated Palace.",
    )
    parser.add_argument("--seed-batch-size", type=int, default=256)
    parser.add_argument("--queries", type=int, default=16)
    parser.add_argument("--write-samples", type=int, default=8)
    parser.add_argument("--concurrencies", default="1,2,4,8")
    parser.add_argument("--mixed-operations", type=int, default=32)
    parser.add_argument("--privacy-page-size", type=int, default=5_000)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Bypass LockedBackend for isolated read-only A/B diagnostics.",
    )
    parser.add_argument("--normal-shared-embedding", action="store_true")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--long-term-records-per-day",
        type=int,
        default=10,
        help="Convert each size into an approximate accumulation horizon.",
    )
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--qdrant-namespace", default="eidolon-scale")
    parser.add_argument("--qdrant-timeout", type=float, default=10.0)
    parser.add_argument("--output", help="Write the JSON report to this path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rendered = json.dumps(asyncio.run(_run(args)), indent=2, ensure_ascii=False)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(json.dumps({"output": str(output)}, ensure_ascii=False))
        return
    print(rendered)


if __name__ == "__main__":
    main()
