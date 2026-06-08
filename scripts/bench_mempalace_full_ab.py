from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.palace_graph import build_palace_graph
from eidolon.memory.application.public_recall import (
    recall_with_kg_fusion,
    search_all_wings_mcp_style,
)
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

DATASET = [
    {
        "kind": "short_en",
        "wing": "Wing_Work",
        "room": "backend",
        "marker": "eidolon_marker_short_en_backend",
        "query": "eidolon_marker_short_en_backend backend latency",
        "text": "The memory backend benchmark checks backend latency and recall quality.",
    },
    {
        "kind": "zh_cn",
        "wing": "Wing_Profile",
        "room": "preference",
        "marker": "eidolon_marker_zh_oolong",
        "query": "eidolon_marker_zh_oolong 乌龙茶 偏好",
        "text": "用户偏好乌龙茶，不喜欢太甜的饮料，记忆检索需要稳定准确。",
    },
    {
        "kind": "mixed_multilingual",
        "wing": "Wing_Life",
        "room": "home_lab",
        "marker": "eidolon_marker_mixed_home_lab",
        "query": "eidolon_marker_mixed_home_lab 局域网 desktop qdrant",
        "text": "Home LAN deployment uses desktop agents, Qdrant, and 中文 voice recall.",
    },
    {
        "kind": "long_text",
        "wing": "Wing_Future",
        "room": "roadmap",
        "marker": "eidolon_marker_long_roadmap",
        "query": "eidolon_marker_long_roadmap roadmap stability accuracy",
        "text": " ".join(
            [
                "The roadmap prioritizes fast stable accurate memory retrieval",
                "with read write benchmark evidence and end to end recall checks.",
            ]
            * 45
        ),
    },
    {
        "kind": "markdown",
        "wing": "Wing_Work",
        "room": "notes",
        "marker": "eidolon_marker_markdown_notes",
        "query": "eidolon_marker_markdown_notes benchmark checklist",
        "text": "# Benchmark checklist\n\n- read path\n- write path\n- voice recall\n- KG fusion",
    },
    {
        "kind": "code",
        "wing": "Wing_Work",
        "room": "code",
        "marker": "eidolon_marker_code_snippet",
        "query": "eidolon_marker_code_snippet asyncio lock",
        "text": "async def recall():\n    async with backend.lock:\n        return await search()",
    },
    {
        "kind": "metadata_rich",
        "wing": "Wing_Event",
        "room": "incident",
        "marker": "eidolon_marker_metadata_incident",
        "query": "eidolon_marker_metadata_incident disk io 502",
        "text": "A transient disk I/O and 502 failure appeared during admin memory access.",
    },
    {
        "kind": "private_filter",
        "wing": "Wing_Profile",
        "room": "privacy",
        "marker": "eidolon_marker_private_filter",
        "query": "eidolon_marker_private_filter private should not recall",
        "text": "This private memory should be hidden from public recall.",
        "privacy": "private",
    },
]

KG_FACTS = [
    ("person:Alice", "likes", "oolong_tea"),
    ("person:Alice", "uses", "qdrant"),
    ("person:Alice", "works_at", "home_lab"),
    ("pet:铁锤", "holds_role", "family_pet"),
    ("place:局域网", "has_state", "local_deployment"),
    ("project:Eidolon", "uses", "mempalace"),
    ("project:Eidolon", "has_concern", "sqlite_contention"),
    ("project:Eidolon", "planned_to", "benchmark_backends"),
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
                "kg_in_recall": True,
                "kg_timeout_seconds": 0.2,
                "kg_max_entities": 4,
                "kg_max_triples_per_entity": 8,
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
    latencies: list[float],
    errors: list[str],
    label: str,
    op: Callable[[], Awaitable[Any]],
) -> Any:
    t0 = time.perf_counter()
    try:
        result = await op()
    except Exception as exc:  # noqa: BLE001 - benchmark records failures
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return None
    latencies.append((time.perf_counter() - t0) * 1000)
    return result


def _contains_marker(records: list[Any], marker: str) -> bool:
    for rec in records or []:
        value = getattr(rec, "value", "") or ""
        metadata = getattr(rec, "metadata", {}) or {}
        if marker in value or marker in json.dumps(metadata, ensure_ascii=False):
            return True
    return False


async def _seed_vectors(
    backend: LockedBackend,
    *,
    repeats: int,
) -> tuple[dict[str, list[float]], list[str]]:
    latencies_by_kind: dict[str, list[float]] = {item["kind"]: [] for item in DATASET}
    errors: list[str] = []
    for item in DATASET:
        for idx in range(repeats):
            marker = f"{item['marker']}_{idx}"
            text = f"{marker}\n{item['text']}"
            metadata = {
                "user_id": "bench",
                "source_file": f"bench/{item['kind']}/{idx}.txt",
                "data_type": item["kind"],
                "marker": marker,
                "importance": 4,
                "confidence": 0.95,
                "tags": [item["kind"], "benchmark"],
            }
            if item.get("privacy"):
                metadata["privacy"] = item["privacy"]

            await _timed(
                latencies_by_kind[item["kind"]],
                errors,
                f"seed_vector:{item['kind']}#{idx}",
                lambda item=item, text=text, metadata=metadata: backend.ingest_text(
                    wing=item["wing"],
                    room=item["room"],
                    text=text,
                    metadata=metadata,
                ),
            )
    return latencies_by_kind, errors


async def _seed_kg(kg: LockedKnowledgeGraph, *, repeats: int) -> tuple[list[float], list[str]]:
    latencies: list[float] = []
    errors: list[str] = []
    for idx in range(repeats):
        for subject, predicate, obj in KG_FACTS:
            await _timed(
                latencies,
                errors,
                f"seed_kg:{subject}:{predicate}:{idx}",
                lambda subject=subject, predicate=predicate, obj=obj, idx=idx: kg.add_triple(
                    subject=subject,
                    predicate=predicate,
                    object=f"{obj}_{idx}",
                    source_turn_id=f"bench-kg-{idx}-{subject}-{predicate}-{obj}",
                    adapter_name="benchmark",
                ),
            )
    for alias, entity in [("Alice", "person:Alice"), ("铁锤", "pet:铁锤"), ("局域网", "place:局域网")]:
        await kg.record_entity_mention(
            entity_id=entity,
            alias=alias,
            source="benchmark",
            confidence=1.0,
        )
    return latencies, errors


async def _collect_key_map(backend: LockedBackend) -> dict[str, str]:
    rows = await backend.get_all("", limit=10000)
    out: dict[str, str] = {}
    for row in rows:
        marker = str((row.metadata or {}).get("marker") or "")
        if marker:
            out[marker] = row.key
    return out


async def _bench_backend_reads(
    backend: LockedBackend,
    *,
    key_map: dict[str, str],
    iterations: int,
) -> dict[str, Any]:
    errors: list[str] = []
    get_all_lat: list[float] = []
    get_by_id_lat: list[float] = []
    keys = list(key_map.values())

    for idx in range(iterations):
        await _timed(
            get_all_lat,
            errors,
            f"get_all#{idx}",
            lambda idx=idx: backend.get_all("", limit=25, offset=idx % 5),
        )
        if keys:
            key = keys[idx % len(keys)]
            await _timed(
                get_by_id_lat,
                errors,
                f"get_by_id#{idx}",
                lambda key=key: backend.get("bench", key),
            )

    return {
        "errors": errors,
        "get_all_ms": _summary(get_all_lat),
        "get_by_id_ms": _summary(get_by_id_lat),
    }


async def _bench_deletes(
    backend: LockedBackend,
    *,
    key_map: dict[str, str],
    max_deletes: int = 8,
) -> dict[str, Any]:
    errors: list[str] = []
    delete_lat: list[float] = []
    keys = list(key_map.values())[:max_deletes]
    for key in keys:
        await _timed(
            delete_lat,
            errors,
            f"delete:{key}",
            lambda key=key: backend.delete("bench", key),
        )
    return {
        "errors": errors,
        "delete_ms": _summary(delete_lat),
        "deleted": len(delete_lat),
    }


async def _bench_recall(
    backend: LockedBackend,
    kg: LockedKnowledgeGraph,
    settings: MemorySettings,
    *,
    palace_path: str,
    iterations: int,
    seed_repeats: int,
    top_k: int,
) -> dict[str, Any]:
    errors: list[str] = []
    normal_lat: list[float] = []
    voice_lat: list[float] = []
    fusion_lat: list[float] = []
    low_level_lat: list[float] = []
    normal_hits = 0
    voice_hits = 0
    fusion_vector_hits = 0
    fusion_kg_hits = 0
    private_leaks = 0
    by_type = {item["kind"]: {"normal": 0, "voice": 0, "fusion": 0} for item in DATASET}

    public_items = [item for item in DATASET if item.get("privacy") != "private"]
    for idx in range(iterations):
        item = public_items[idx % len(public_items)]
        marker = f"{item['marker']}_{idx % max(1, seed_repeats)}"
        query = f"{item['query']} {marker}"

        normal = await _timed(
            normal_lat,
            errors,
            f"normal_recall:{item['kind']}#{idx}",
            lambda query=query: search_all_wings_mcp_style(
                backend,
                settings,
                query=query,
                user_id="bench",
                top_k=top_k,
                wing=None,
                room=None,
                for_voice=False,
                palace_path=palace_path,
            ),
        )
        if _contains_marker(normal or [], item["marker"]):
            normal_hits += 1
            by_type[item["kind"]]["normal"] += 1
        if _contains_marker(normal or [], "eidolon_marker_private_filter"):
            private_leaks += 1

        voice = await _timed(
            voice_lat,
            errors,
            f"voice_recall:{item['kind']}#{idx}",
            lambda query=query: search_all_wings_mcp_style(
                backend,
                settings,
                query=query,
                user_id="bench",
                top_k=top_k,
                wing=None,
                room=None,
                for_voice=True,
                palace_path=palace_path,
            ),
        )
        if _contains_marker(voice or [], item["marker"]):
            voice_hits += 1
            by_type[item["kind"]]["voice"] += 1
        if _contains_marker(voice or [], "eidolon_marker_private_filter"):
            private_leaks += 1

        fused = await _timed(
            fusion_lat,
            errors,
            f"fusion_recall:{item['kind']}#{idx}",
            lambda query=query: recall_with_kg_fusion(
                backend,
                settings,
                query=f"{query} Alice Eidolon 铁锤 局域网",
                user_id="bench",
                top_k=top_k,
                kg=kg,
                for_voice=False,
                palace_path=palace_path,
            ),
        )
        vector_rows = (fused or {}).get("vector", []) if isinstance(fused, dict) else []
        kg_rows = (fused or {}).get("kg", []) if isinstance(fused, dict) else []
        if _contains_marker(vector_rows, item["marker"]):
            fusion_vector_hits += 1
            by_type[item["kind"]]["fusion"] += 1
        if kg_rows:
            fusion_kg_hits += 1

        await _timed(
            low_level_lat,
            errors,
            f"backend_search:{item['kind']}#{idx}",
            lambda item=item, query=query: backend.search(
                query,
                wing=item["wing"],
                n_results=top_k,
                room=None,
            ),
        )

    denom = max(1, iterations)
    return {
        "errors": errors,
        "private_leaks": private_leaks,
        "normal_ms": _summary(normal_lat),
        "voice_ms": _summary(voice_lat),
        "fusion_ms": _summary(fusion_lat),
        "backend_search_ms": _summary(low_level_lat),
        "hit_rate": {
            "normal": normal_hits / denom,
            "voice": voice_hits / denom,
            "fusion_vector": fusion_vector_hits / denom,
            "fusion_kg_nonempty": fusion_kg_hits / denom,
        },
        "by_type_hits": by_type,
    }


async def _bench_admin_views(
    backend: LockedBackend,
    kg: LockedKnowledgeGraph,
    *,
    palace_path: str,
    iterations: int,
) -> dict[str, Any]:
    errors: list[str] = []
    graph_lat: list[float] = []
    kg_snapshot_lat: list[float] = []
    kg_stats_lat: list[float] = []
    graph_available = 0
    snapshot_nonempty = 0

    for idx in range(iterations):
        graph = await _timed(
            graph_lat,
            errors,
            f"palace_graph#{idx}",
            lambda: build_palace_graph(
                backend,
                palace_path=palace_path,
                max_nodes=100,
                max_edges=200,
            ),
        )
        if isinstance(graph, dict) and graph.get("available"):
            graph_available += 1

        snap = await _timed(
            kg_snapshot_lat,
            errors,
            f"kg_snapshot#{idx}",
            lambda: kg.timeline(limit=400),
        )
        if snap:
            snapshot_nonempty += 1

        await _timed(kg_stats_lat, errors, f"kg_stats#{idx}", kg.stats)

    return {
        "errors": errors,
        "palace_graph_ms": _summary(graph_lat),
        "kg_snapshot_ms": _summary(kg_snapshot_lat),
        "kg_stats_ms": _summary(kg_stats_lat),
        "graph_available_rate": graph_available / max(1, iterations),
        "kg_snapshot_nonempty_rate": snapshot_nonempty / max(1, iterations),
    }


async def _bench_concurrency(
    backend: LockedBackend,
    settings: MemorySettings,
    *,
    palace_path: str,
    operations: int,
    concurrency: int,
    top_k: int,
) -> dict[str, Any]:
    sem = asyncio.Semaphore(concurrency)
    errors: list[str] = []
    latencies: dict[str, list[float]] = {"normal": [], "voice": [], "write": [], "get_all": []}
    empty_reads = 0

    async def one(idx: int) -> None:
        nonlocal empty_reads
        if idx % 7 == 0:
            kind = "write"
        elif idx % 5 == 0:
            kind = "get_all"
        else:
            kind = "voice" if idx % 2 else "normal"
        async with sem:
            async def op() -> Any:
                nonlocal empty_reads
                if kind == "write":
                    return await backend.ingest_text(
                        wing=WINGS[idx % len(WINGS)],
                        room=f"concurrent_{idx % 9}",
                        text=f"eidolon_marker_concurrent_{idx} concurrent mixed write benchmark",
                        metadata={
                            "user_id": "bench",
                            "source_file": f"bench/concurrent/{idx}.txt",
                            "marker": f"eidolon_marker_concurrent_{idx}",
                        },
                    )
                if kind == "get_all":
                    rows = await backend.get_all("", limit=20, offset=idx % 10)
                    if not rows:
                        empty_reads += 1
                    return rows
                rows = await search_all_wings_mcp_style(
                    backend,
                    settings,
                    query=f"eidolon_marker_short_en_backend concurrent query {idx}",
                    user_id="bench",
                    top_k=top_k,
                    wing=None,
                    room=None,
                    for_voice=(kind == "voice"),
                    palace_path=palace_path,
                )
                if not rows:
                    empty_reads += 1
                return rows

            await _timed(latencies[kind], errors, f"concurrent:{kind}#{idx}", op)

    started = time.perf_counter()
    await asyncio.gather(*(one(idx) for idx in range(operations)))
    return {
        "errors": errors[:20],
        "error_count": len(errors),
        "empty_reads": empty_reads,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "latency_ms": {key: _summary(value) for key, value in latencies.items()},
    }


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

    backend = LockedBackend(MemPalacePythonBackend(settings, str(palace)))
    from mempalace.knowledge_graph import KnowledgeGraph

    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(palace / "knowledge_graph.sqlite3")),
        backend.lock,
    )

    started = time.perf_counter()
    vector_write_by_type, vector_write_errors = await _seed_vectors(backend, repeats=args.seed_repeats)
    kg_write_lat, kg_write_errors = await _seed_kg(kg, repeats=args.kg_repeats)
    key_map = await _collect_key_map(backend)

    backend_reads = await _bench_backend_reads(
        backend,
        key_map=key_map,
        iterations=args.read_iterations,
    )
    recall = await _bench_recall(
        backend,
        kg,
        settings,
        palace_path=str(palace),
        iterations=args.recall_iterations,
        seed_repeats=args.seed_repeats,
        top_k=args.top_k,
    )
    admin_views = await _bench_admin_views(
        backend,
        kg,
        palace_path=str(palace),
        iterations=args.admin_iterations,
    )
    concurrency = await _bench_concurrency(
        backend,
        settings,
        palace_path=str(palace),
        operations=args.concurrent_operations,
        concurrency=args.concurrency,
        top_k=args.top_k,
    )
    deletes = await _bench_deletes(backend, key_map=key_map)

    kg.close()
    return {
        "backend": backend_name,
        "palace": str(palace),
        "seed": {
            "vector_records": len(DATASET) * args.seed_repeats,
            "kg_triples": len(KG_FACTS) * args.kg_repeats,
            "vector_write_errors": vector_write_errors,
            "kg_write_errors": kg_write_errors,
            "vector_write_ms_by_type": {
                key: _summary(values) for key, values in vector_write_by_type.items()
            },
            "kg_write_ms": _summary(kg_write_lat),
            "key_count": len(key_map),
        },
        "backend_reads": backend_reads,
        "recall": recall,
        "admin_views": admin_views,
        "concurrency": concurrency,
        "deletes": deletes,
        "total_elapsed_ms": (time.perf_counter() - started) * 1000,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Comprehensive caller-level MemPalace backend benchmark.")
    parser.add_argument("--backend", choices=["chroma", "qdrant"], required=True)
    parser.add_argument("--palace", required=True)
    parser.add_argument("--seed-repeats", type=int, default=10)
    parser.add_argument("--kg-repeats", type=int, default=6)
    parser.add_argument("--read-iterations", type=int, default=30)
    parser.add_argument("--recall-iterations", type=int, default=28)
    parser.add_argument("--admin-iterations", type=int, default=10)
    parser.add_argument("--concurrent-operations", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--qdrant-namespace", default="eidolon-full-ab")
    parser.add_argument("--qdrant-timeout", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(asyncio.run(_run(parse_args())), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
