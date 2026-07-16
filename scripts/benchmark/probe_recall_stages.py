#!/usr/bin/env python3
"""Probe each stage of LiveKit-voice recall to localize first-call jitter.

In-process (avoids MCP HTTP framing noise) reproduces the same code path the
agent_runner would run, but with timing breakpoints around:

  * embedding (ONNX) ─ LRU cache hit / miss
  * asyncio.Lock acquire
  * mempalace fast-search (collection.query over each wing)
  * result filter / rank

The script runs N calls against a seeded palace and prints per-call breakdown
for the first ``--cold-rounds`` calls plus aggregate percentiles for the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eidolon.memory.adapters.locked_backend import LockedBackend  # noqa: E402
from eidolon.memory.adapters.mempalace_python_backend import (  # noqa: E402
    MemPalacePythonBackend,
)
from eidolon.memory.adapters.mempalace_query_embedding import (  # noqa: E402
    _embed_query_cached_normalized,
    clear_embedding_cache,
)
from eidolon.memory.config.memory_settings import get_memory_settings  # noqa: E402
from eidolon.memory.config.palace_directory import (  # noqa: E402
    resolve_palace_for_user,
)

_QUERIES = [
    "用户最近的情绪状态",
    "用户喜欢什么音乐",
    "工作压力",
    "和家人的关系",
    "最近的健康状况",
    "兴趣爱好",
    "重要的事件",
    "生活习惯和偏好",
]


def _percentiles(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "min": 0}
    s = sorted(samples)
    n = len(s)
    def pct(p):
        idx = min(n - 1, max(0, int(p * n) - 1))
        return round(s[idx], 3)
    return {
        "count": n,
        "min": round(s[0], 3),
        "p50": pct(0.50),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": round(s[-1], 3),
        "mean": round(statistics.mean(s), 3),
    }


async def _timed_recall(
    backend: LockedBackend,
    settings,
    *,
    palace_path: str,
    user_id: str,
    query: str,
) -> dict:
    """Run a single voice recall, reporting per-stage timings (ms)."""
    from eidolon.memory.adapters.mempalace_fast_search import search_memories_shared_embedding
    from eidolon.memory.adapters.search_payload import parse_search_tool_payload
    from eidolon.memory.application.public_recall import (
        _resolve_wings,
        filter_voice_recall_hits,
        rank_records_by_similarity,
        recall_record_visible_for_user,
    )

    stages: dict[str, float] = {}

    wings = _resolve_wings(settings, wing=None, for_voice=True)
    top_k = settings.recall.top_k

    # ── embed ──
    t0 = time.perf_counter()
    cache_info_before = _embed_query_cached_normalized.cache_info()
    # We can call the cached function directly here; production calls embed
    # via mempalace.embedding inside search_memories_shared_embedding, but
    # measuring the cached path is the cheapest representative.
    from eidolon.memory.adapters.mempalace_query_embedding import embed_query_vector
    _vec = embed_query_vector(query)
    del _vec
    stages["embed_ms"] = (time.perf_counter() - t0) * 1000
    cache_info_after = _embed_query_cached_normalized.cache_info()
    stages["embed_cache_hit"] = (
        cache_info_after.hits > cache_info_before.hits
    )

    # ── lock acquire ──
    t1 = time.perf_counter()
    async with backend.lock:
        stages["lock_acquire_ms"] = (time.perf_counter() - t1) * 1000
        # ── vector query (in-thread, blocking) ──
        t2 = time.perf_counter()
        raw_hits = await asyncio.to_thread(
            search_memories_shared_embedding,
            query,
            palace_path,
            wings=wings,
            room=None,
            n_results=top_k,
            skip_closets=settings.runtime.read.voice_skip_closets,
        )
        stages["vector_query_ms"] = (time.perf_counter() - t2) * 1000

    # ── filter + rank ──
    t3 = time.perf_counter()
    records = parse_search_tool_payload({"results": raw_hits})
    records = [
        r for r in records if recall_record_visible_for_user(r, user_id)
    ]
    records = filter_voice_recall_hits(records, settings)
    records = rank_records_by_similarity(records, top_k=top_k)
    stages["filter_rank_ms"] = (time.perf_counter() - t3) * 1000

    stages["hits"] = len(records)
    stages["total_ms"] = sum(
        stages[k] for k in ("embed_ms", "lock_acquire_ms", "vector_query_ms", "filter_rank_ms")
    )
    return stages


async def _main(args):
    settings = get_memory_settings()
    palace_path = resolve_palace_for_user(settings, args.user_id)
    inner = MemPalacePythonBackend(settings, str(palace_path))
    backend = LockedBackend(inner)

    # Main-thread warm of chromadb so the threadpool inside
    # mempalace_fast_search doesn't see an uninitialized RustBindingsAPI on
    # its first hit. We DON'T reset the LRU embedding cache here yet.
    print(f"[probe] palace={palace_path}")
    print("[probe] warming chromadb client + ONNX embedder on main thread…")
    await inner.search("warmup", wing="Wing_Profile", n_results=1)

    if args.clear_cache:
        clear_embedding_cache()
        print("[probe] cleared embedding LRU cache (post-warm)")

    print(f"[probe] count={args.count} cold_rounds={args.cold_rounds}")

    all_stages: list[dict] = []
    for i in range(args.count):
        q = _QUERIES[i % len(_QUERIES)]
        stages = await _timed_recall(
            backend,
            settings,
            palace_path=str(palace_path),
            user_id=args.user_id,
            query=q,
        )
        stages["iter"] = i
        stages["query"] = q
        all_stages.append(stages)
        if i < args.cold_rounds:
            print(
                f"  [{i:03d}] embed={stages['embed_ms']:6.2f}ms "
                f"(cache_hit={stages['embed_cache_hit']}) "
                f"lock={stages['lock_acquire_ms']:5.2f}ms "
                f"vector={stages['vector_query_ms']:6.2f}ms "
                f"filter={stages['filter_rank_ms']:5.2f}ms "
                f"total={stages['total_ms']:6.2f}ms "
                f"hits={stages['hits']}"
            )

    # Aggregate over WARM samples (skip cold rounds)
    warm = all_stages[args.cold_rounds:]
    aggregate = {
        "n_total": len(all_stages),
        "n_cold": args.cold_rounds,
        "n_warm": len(warm),
        "warm_breakdown_ms": {
            "embed": _percentiles([s["embed_ms"] for s in warm]),
            "lock_acquire": _percentiles([s["lock_acquire_ms"] for s in warm]),
            "vector_query": _percentiles([s["vector_query_ms"] for s in warm]),
            "filter_rank": _percentiles([s["filter_rank_ms"] for s in warm]),
            "total": _percentiles([s["total_ms"] for s in warm]),
        },
        "cold_breakdown_ms": {
            "embed": _percentiles([s["embed_ms"] for s in all_stages[: args.cold_rounds]]),
            "lock_acquire": _percentiles(
                [s["lock_acquire_ms"] for s in all_stages[: args.cold_rounds]]
            ),
            "vector_query": _percentiles(
                [s["vector_query_ms"] for s in all_stages[: args.cold_rounds]]
            ),
            "filter_rank": _percentiles(
                [s["filter_rank_ms"] for s in all_stages[: args.cold_rounds]]
            ),
            "total": _percentiles([s["total_ms"] for s in all_stages[: args.cold_rounds]]),
        },
    }
    print("\n[probe] aggregate")
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(
                {"per_call": all_stages, "aggregate": aggregate},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"[probe] wrote {args.out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="bench")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--cold-rounds", type=int, default=10)
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear the LRU embedding cache before the run.",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    asyncio.run(_main(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
