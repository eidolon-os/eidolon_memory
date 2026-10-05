#!/usr/bin/env python3
"""Measure the production recall entrypoint, in-process, using its own trace.

Uses the normal router/locks and configured embedder. This is a service-stage
probe, not MCP end-to-end latency or a retrieval-quality benchmark. Trace stages
can overlap; total latency is measured at the boundary, never summed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eidolon_memory_contracts import build_memory_actor_context  # noqa: E402

from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter  # noqa: E402
from eidolon.memory.adapters.mempalace_query_embedding import clear_embedding_cache  # noqa: E402
from eidolon.memory.application.public_recall import recall_with_kg_fusion  # noqa: E402
from eidolon.memory.config.memory_settings import get_memory_settings  # noqa: E402
from scripts.benchmark.manifest import build_manifest  # noqa: E402
from scripts.benchmark.report import percentiles  # noqa: E402

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


def _summarize(samples: list[dict]) -> dict:
    stages = sorted({key for sample in samples for key in sample["trace"]})
    return {
        "count": len(samples),
        "degraded": sum(sample["degraded"] for sample in samples),
        "stages_ms": {
            key: percentiles([s["trace"][key] for s in samples if key in s["trace"]])
            for key in stages
        },
    }


async def _main(args):
    settings = get_memory_settings()
    context = build_memory_actor_context(
        memory_realm_id=args.user_id,
        owner_id="benchmark",
        companion_id="benchmark",
        device_id="benchmark",
        session_id="benchmark-probe",
    )
    router = LocalPalaceRouter(settings, allowed_spaces=[args.user_id])
    try:
        runtime = await router.resolve(args.user_id)
        if args.with_kg and runtime.kg is None:
            raise ValueError("--with-kg requires a configured graph backend")
        manifest = build_manifest(
            suite="probe-recall-stages",
            repo_root=_ROOT,
            settings=settings,
            palace_path=runtime.palace_path,
            command=shlex.join([sys.executable, *sys.argv]),
            scale={"count": args.count, "warmup": args.warmup},
            notes="In-process production recall; warmup separate; not an MCP or quality gate.",
        )
        queries = args.query or _QUERIES
        samples = []
        for i in range(args.warmup + args.count):
            # Cold-query mode includes embedding work on every request. It does
            # not reload model weights or pretend to measure process cold start.
            if args.clear_cache:
                clear_embedding_cache()
            query = queries[i % len(queries)]
            result = await recall_with_kg_fusion(
                runtime.backend,
                settings,
                query=query,
                context=context,
                top_k=settings.recall.top_k,
                kg=runtime.kg if args.with_kg else None,
                for_voice=not args.chat,
                palace_path=str(runtime.palace_path),
            )
            samples.append({
                "query": query,
                "warmup": i < args.warmup,
                "trace": result["trace"],
                "degraded": result["degraded"],
                "degraded_reason": result["degraded_reason"],
                "vector_keys": [r.metadata.get("_storage_id", r.key) for r in result["vector"]],
                "kg_ids": [r.id for r in result["kg"]],
            })
        output = {
            "manifest": manifest,
            "settings": {"voice": not args.chat, "with_kg": args.with_kg,
                         "clear_cache_each_call": args.clear_cache},
            "warmup": _summarize(samples[:args.warmup]),
            "aggregate": _summarize(samples[args.warmup:]),
            "per_call": samples,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(output["aggregate"], indent=2))
        print(f"[probe] wrote {args.out}")
        return 1 if any(sample["degraded"] for sample in samples) else 0
    finally:
        await router.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="bench")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--chat", action="store_true", help="Measure chat instead of voice.")
    parser.add_argument("--with-kg", action="store_true")
    parser.add_argument("--query", action="append", help="Repeat for a fixed query corpus.")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Clear the query embedding cache before each call.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.count < 1 or args.warmup < 0:
        parser.error("count must be positive and warmup must be nonnegative")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
