#!/usr/bin/env python3
"""R-01: recall latency through a running production MCP Agent surface.

Reuses one MCP session. Warmup, failures and raw samples remain in the report.
The caller owns the server lifecycle and supplies its provenance; this client
cannot infer a remote server's version or data from its own checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
from eidolon_memory_contracts import MemoryActorContext, build_memory_actor_context  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from scripts.benchmark.manifest import code_provenance, machine_facts, utc_stamp  # noqa: E402
from scripts.benchmark.mcp_response import decode_recall_response as _decode_recall  # noqa: E402
from scripts.benchmark.report import percentiles  # noqa: E402

_DEFAULT_QUERIES = [
    "用户最近的情绪状态",
    "用户喜欢什么音乐",
    "工作压力",
    "和家人的关系",
    "最近的健康状况",
    "兴趣爱好",
    "重要的事件",
    "生活习惯和偏好",
]


def _local_http_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False)


def _summarize(samples: list[dict]) -> dict:
    good = [s for s in samples if s["error"] is None and not s["degraded"]]
    return {
        "count": len(samples),
        "errors": sum(s["error"] is not None for s in samples),
        "degraded": sum(s["degraded"] for s in samples),
        "empty": sum(not s["records"] and not s["kg_triples"] for s in good),
        "latency_ms": percentiles([s["elapsed_ms"] for s in samples]),
        "successful_latency_ms": percentiles([s["elapsed_ms"] for s in good]),
    }


async def _run(
    url: str,
    *,
    count: int,
    queries: list[str],
    voice: bool,
    with_kg: bool,
    context: MemoryActorContext,
    warmup: int = 8,
    timeout_s: float = 10,
) -> dict:
    if count < 1 or warmup < 0 or not queries or timeout_s <= 0:
        raise ValueError("positive count/timeout and nonempty queries are required")
    samples = []
    startup_error = None
    try:
        async with streamablehttp_client(url, httpx_client_factory=_local_http_client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as sess:
                await asyncio.wait_for(sess.initialize(), timeout_s)
                for i in range(warmup + count):
                    query = queries[i % len(queries)]
                    sample = {
                        "query": query,
                        "warmup": i < warmup,
                        "error": None,
                        "degraded": False,
                        "degraded_reason": None,
                        "records": [],
                        "kg_triples": [],
                        "trace": {},
                    }
                    started = time.perf_counter()
                    try:
                        result = await asyncio.wait_for(
                            sess.call_tool(
                                "eidolon_memory_recall_context",
                                arguments={
                                    "query": query,
                                    "context": context.model_dump(mode="json"),
                                    "top_k": 5,
                                    "voice": voice,
                                    "include_kg": with_kg,
                                },
                            ),
                            timeout_s,
                        )
                        data = _decode_recall(result)
                        sample.update(
                            {
                                key: data.get(key)
                                for key in (
                                    "records",
                                    "kg_triples",
                                    "trace",
                                    "degraded",
                                    "degraded_reason",
                                )
                            }
                        )
                    except Exception as exc:
                        sample["error"] = f"{type(exc).__name__}: {exc}"
                    sample["elapsed_ms"] = (time.perf_counter() - started) * 1000
                    samples.append(sample)
    except Exception as exc:
        startup_error = f"{type(exc).__name__}: {exc}"
    summary = _summarize([s for s in samples if not s["warmup"]])
    warmup_summary = _summarize([s for s in samples if s["warmup"]])
    budget = 60 if voice else 250 if with_kg else 200
    healthy = (
        startup_error is None
        and summary["count"] == count
        and all(s["error"] is None and not s["degraded"] for s in samples)
    )
    return {
        "id": "R-01",
        "url": url,
        "context": context.model_dump(mode="json"),
        "mode": ("voice" if voice else "chat") + ("+kg" if with_kg else ""),
        "n": count,
        "startup_error": startup_error,
        "summary": summary,
        "warmup": warmup_summary,
        "per_call": samples,
        "sla_p95_ms": budget,
        "sla": "PASS" if healthy and summary["latency_ms"]["p95"] <= budget else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8030/mcp")
    parser.add_argument("--count", type=int, default=160)
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--memory-realm-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--companion-id", required=True)
    parser.add_argument("--device-id", default="benchmark")
    parser.add_argument("--session-id", default="benchmark-session")
    parser.add_argument("--query", action="append")
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--with-kg", action="store_true")
    parser.add_argument(
        "--server-manifest",
        type=Path,
        help="Provenance supplied by the operator of the measured server.",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.count < 1 or args.warmup < 0 or args.timeout <= 0:
        parser.error("count/timeout must be positive and warmup nonnegative")
    server_manifest = json.loads(args.server_manifest.read_text()) if args.server_manifest else None
    context = build_memory_actor_context(
        memory_realm_id=args.memory_realm_id,
        owner_id=args.owner_id,
        companion_id=args.companion_id,
        device_id=args.device_id,
        session_id=args.session_id,
    )
    started_at = utc_stamp()
    row = asyncio.run(
        _run(
            args.url,
            count=args.count,
            warmup=args.warmup,
            timeout_s=args.timeout,
            queries=args.query or _DEFAULT_QUERIES,
            voice=args.voice,
            with_kg=args.with_kg,
            context=context,
        )
    )
    row["manifest"] = {
        "started_at": started_at,
        "command": shlex.join([sys.executable, *sys.argv]),
        "client_code": code_provenance(_ROOT),
        "client_machine": machine_facts(),
        "server": server_manifest,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: row[k] for k in ("mode", "sla", "summary", "warmup")}, indent=2))
    return 0 if row["sla"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
