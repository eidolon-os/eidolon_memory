"""Re-measure, on this machine, every number that does not transfer from another.

``docs/ARCHITECTURE.md`` says it plainly: the ordering between embedders transfers
and the resident memory transfers almost exactly, but the absolute milliseconds do
not. Everything this project has measured came from a 12-core Apple Silicon laptop.
The deployment target is a Raspberry Pi 5 — four Cortex-A76 cores at 2.4 GHz,
sharing 4 GB with the rest of Eidolon — where three of the derived bounds change by
a factor of three and the write path is the thing most likely to move.

So this is the board bring-up probe. One command, no NATS, no LLM, no MCP:

    uv run python benchmarks/suites/probe_host.py \\
        --out benchmarks/results/host-profile/pi5.json \\
        --baseline benchmarks/results/host-profile/apple-m-12core.json

It writes a JSON profile and, given a baseline, prints this host beside it with the
ratio. The ratio is the point — a bare "19.9 ms" from the board says nothing without
knowing it was 19.9 ms here too, or 60 ms.

What it deliberately does **not** measure: retrieval quality. That needs the LLM
steward and twenty minutes, it does not vary by host, and
``bench_memory_retrieve_quality`` already owns it. This is the cost profile only.

Everything runs through the real router, the real ``LockedBackend`` and the real
embedder, because measuring a hand-rolled copy of the write path is how you learn
what your benchmark does rather than what your service does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "4")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hostinfo import describe, rss_mb  # noqa: E402

SPACE = "default.probe.default"
OWNER = "owner"

_DOCS = [
    "用户喜欢喝乌龙茶，不加糖，尤其是下午",
    "用户养了一只叫铁锤的鸟，会说几句话",
    "用户在一家做服务机器人的公司做后端",
    "用户的妈妈叫张丽，住在杭州西湖区",
    "用户计划明年春天去日本看樱花",
    "用户最近工作压力比较大，睡得晚",
]
_QUERIES = [
    "我喜欢什么茶",
    "我养了什么宠物",
    "我在哪里工作",
    "我的家人是谁",
    "我计划去哪里",
    "我最近心情怎么样",
]


def _settings_yaml(root: Path, model: str, threads: int, model_dir: str) -> Path:
    """A throwaway configuration, so the probe never touches config/settings.yaml.

    ``steward.mode: rules`` and no NATS section that matters: nothing here publishes
    a turn or calls an LLM, which is what makes this runnable on a board with no
    broker and no API key.

    ``model_dir`` is the other half of that: with it empty the encoder resolves its
    weights through ``hf_hub_download``, which on the Pi means either a slow mirror
    or no network at all. Pointing at weights already on the board is what makes the
    probe runnable there, and the board is the host we actually want measured.
    """

    path = root / "probe-settings.yaml"
    path.write_text(
        "\n".join(
            [
                "runtime:",
                f"  palaces_root: {root / 'palaces'}",
                "mempalace:",
                "  backend: chroma",
                "embedding:",
                "  provider: local",
                f"  model: {model}",
                f"  model_dir: {model_dir}",
                "  device: cpu",
                f"  threads: {threads}",
                "llm:",
                "  model: openai/deepseek-v4-flash",
                "steward:",
                "  mode: rules",
                "kg:",
                "  backend: sqlite",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _fragment(content: str, i: int):
    from eidolon.memory.domain.fragments import MemoryFragment

    return MemoryFragment(
        memory_space_id=SPACE,
        source_turn_id=f"probe-{i}",
        wing="Wing_Life",
        # Unique content per call. The drawer id hashes (wing, room, content), so a
        # reused text turns an insert into an update of the same row and measures a
        # materially cheaper operation than the one being timed. That mistake made
        # a six-fragment turn look cheaper than a single write.
        room="probe",
        content=f"{content}#{i}",
        memory_type="event",
        confidence=0.9,
        importance=3,
    )


def _p(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, int(len(ordered) * q) - 1)], 2)


async def _measure(fragments: int, fanout: int, rounds: int) -> dict:
    from eidolon.memory.adapters.mempalace_query_embedding import clear_embedding_cache
    from eidolon.memory.adapters.space_routing import build_space_router
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.infrastructure.embedder_factory import active_embedder

    settings = get_memory_settings()
    out: dict = {"host": describe(), "embedding_model": settings.embedding.model}

    baseline_rss = rss_mb()
    router = build_space_router(settings, allowed_spaces=None)
    runtime = await router.resolve(SPACE)
    backend, kg = runtime.backend, runtime.kg
    embedder = active_embedder()

    # ── the model, loaded ────────────────────────────────────────────────────
    # Read before the warm-up: the palace is open and the weights are not, which
    # is exactly the footprint of a process configured with ``provider: http``.
    # The gap between this and process_fixed is what an out-of-process embedder
    # would buy per user, and that is the whole capacity question on a board.
    out["rss_palace_open_model_cold_mb"] = rss_mb()
    embedder.embed_documents(["预热"])
    out["process_fixed_rss_mb"] = rss_mb()
    out["import_only_rss_mb"] = baseline_rss

    # ── marginal cost of holding another space ───────────────────────────────
    before_spaces = rss_mb()
    for i in range(3):
        await router.resolve(f"default.probe{i}.default")
    out["rss_per_extra_space_mb"] = round((rss_mb() - before_spaces) / 3, 2)
    out["spaces_held"] = len(router.held_spaces())

    # ── the embedder ─────────────────────────────────────────────────────────
    singles = []
    for i in range(20):
        t = time.perf_counter()
        await asyncio.to_thread(embedder.embed_queries, [f"{_QUERIES[i % 6]}#{i}"])
        singles.append((time.perf_counter() - t) * 1000)
    out["embed_query_p50_ms"] = _p(singles, 0.5)
    out["embed_query_p95_ms"] = _p(singles, 0.95)

    batches = []
    for i in range(10):
        t = time.perf_counter()
        await asyncio.to_thread(embedder.embed_documents, [f"{d}#{i}" for d in _DOCS])
        batches.append((time.perf_counter() - t) * 1000)
    out["embed_6_docs_p50_ms"] = _p(batches, 0.5)

    # ── the write path ───────────────────────────────────────────────────────
    ones = []
    for i in range(10):
        t = time.perf_counter()
        await backend.ingest_fragment(_fragment(_DOCS[i % 6], 100 + i))
        ones.append((time.perf_counter() - t) * 1000)
    out["write_one_fragment_p50_ms"] = _p(ones, 0.5)

    batched, looped = [], []
    for r in range(5):
        t = time.perf_counter()
        await backend.ingest_fragments(
            [_fragment(_DOCS[i % 6], 1000 + r * 50 + i) for i in range(fragments)]
        )
        batched.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        for i in range(fragments):
            await backend.ingest_fragment(_fragment(_DOCS[i % 6], 2000 + r * 50 + i))
        looped.append((time.perf_counter() - t) * 1000)
    out["write_turn_batched_p50_ms"] = _p(batched, 0.5)
    out["write_turn_looped_p50_ms"] = _p(looped, 0.5)

    # ── recall ───────────────────────────────────────────────────────────────
    reads = []
    for i in range(rounds):
        clear_embedding_cache()
        t = time.perf_counter()
        await backend.search_scoped(f"{_QUERIES[i % 6]}#{i}", wings=["Wing_Life"], n_results=5)
        reads.append((time.perf_counter() - t) * 1000)
    out["recall_p50_ms"] = _p(reads, 0.5)
    out["recall_p95_ms"] = _p(reads, 0.95)
    out["documents_in_palace"] = len(await backend.get_all(SPACE))

    # ── the readers-writer lock, on the case it was introduced for ───────────
    #
    # A graph read issued at the same moment as a vector search: what
    # recall_with_kg_fusion does, on a 50 ms voice budget. Under the exclusive mutex
    # this replaced, the graph waited out the whole vector search.
    if kg is not None:
        for i, (s, p, o) in enumerate([("用户", "likes", "乌龙茶"), ("用户", "owns", "铁锤")]):
            await kg.add_triple(
                subject=s, predicate=p, object=o, audience=OWNER, source_turn_id=f"probe-t{i}"
            )
        graph_waits = []
        for i in range(rounds):
            clear_embedding_cache()

            async def _vector(i=i) -> None:
                await backend.search_scoped(
                    f"{_QUERIES[i % 6]}@{i}", wings=["Wing_Life"], n_results=5
                )

            async def _graph() -> float:
                t = time.perf_counter()
                await kg.query_entity("用户", audiences=(OWNER,))
                return (time.perf_counter() - t) * 1000

            _v, waited = await asyncio.gather(_vector(), _graph())
            graph_waits.append(waited)
        out["graph_beside_vector_p50_ms"] = _p(graph_waits, 0.5)
        out["graph_beside_vector_p95_ms"] = _p(graph_waits, 0.95)
        out["voice_graph_budget_ms"] = settings.recall.kg_timeout_seconds * 1000

    # ── concurrent reads ─────────────────────────────────────────────────────
    clear_embedding_cache()
    t = time.perf_counter()
    await asyncio.gather(
        *[
            backend.search_scoped(f"并发{i}", wings=["Wing_Life"], n_results=5)
            for i in range(fanout)
        ]
    )
    out[f"recall_x{fanout}_concurrent_ms"] = round((time.perf_counter() - t) * 1000, 2)

    out["peak_rss_mb"] = rss_mb()
    await router.aclose()
    return out


_ROWS = [
    ("cpu_count", "cores", "{}"),
    ("total_ram_mb", "RAM MB", "{}"),
    ("ledger_semaphore", "ledger semaphore", "{}"),
    ("default_executor_threads", "executor threads", "{}"),
    ("import_only_rss_mb", "RSS: imports only", "{} MB"),
    ("rss_palace_open_model_cold_mb", "RSS: + palace open", "{} MB"),
    ("process_fixed_rss_mb", "RSS: + model loaded", "{} MB"),
    ("rss_per_extra_space_mb", "RSS: per extra space", "{} MB"),
    ("peak_rss_mb", "RSS: peak", "{} MB"),
    ("embed_query_p50_ms", "embed 1 query p50", "{} ms"),
    ("embed_query_p95_ms", "embed 1 query p95", "{} ms"),
    ("embed_6_docs_p50_ms", "embed 6 docs p50", "{} ms"),
    ("write_one_fragment_p50_ms", "write 1 fragment", "{} ms"),
    ("write_turn_batched_p50_ms", "write turn (batched)", "{} ms"),
    ("write_turn_looped_p50_ms", "write turn (one by one)", "{} ms"),
    ("recall_p50_ms", "recall p50", "{} ms"),
    ("recall_p95_ms", "recall p95", "{} ms"),
    ("graph_beside_vector_p50_ms", "graph beside vector p50", "{} ms"),
    ("graph_beside_vector_p95_ms", "graph beside vector p95", "{} ms"),
]


def _flat(profile: dict) -> dict:
    merged = dict(profile.get("host") or {})
    merged.update({k: v for k, v in profile.items() if k != "host"})
    return merged


def report(profile: dict, baseline: dict | None) -> None:
    here = _flat(profile)
    there = _flat(baseline) if baseline else {}
    host = profile["host"]
    print(f"\nhost   {host['cpu']} · {host['machine']} · python {host['python']}")
    print(f"model  {profile['embedding_model']}  ({profile.get('documents_in_palace', 0)} docs)")
    if baseline:
        b = baseline["host"]
        print(f"vs     {b['cpu']} · {b['machine']}")
    print()

    width = max(len(label) for _, label, _ in _ROWS)
    header = f"  {'':<{width}}  {'this host':>12}"
    if baseline:
        header += f"  {'baseline':>12}  {'ratio':>7}"
    print(header)
    for key, label, fmt in _ROWS:
        if key not in here or here[key] is None:
            continue
        line = f"  {label:<{width}}  {fmt.format(here[key]):>12}"
        if baseline and there.get(key):
            ratio = here[key] / there[key] if there[key] else 0
            line += f"  {fmt.format(there[key]):>12}  {ratio:>6.2f}x"
        print(line)

    budget = here.get("voice_graph_budget_ms")
    graph = here.get("graph_beside_vector_p95_ms")
    if budget and graph:
        verdict = "fits" if graph < budget else "BLOWS"
        print(f"\n  graph lookup {verdict} the {budget:.0f} ms voice budget (p95 {graph} ms)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="", help="override embedding.model")
    parser.add_argument(
        "--model-dir",
        default="",
        help="weights already on this host, so the probe needs no network",
    )
    parser.add_argument(
        "--threads", type=int, default=0, help="embedding.threads (0 = ORT default)"
    )
    parser.add_argument("--fragments", type=int, default=6, help="fragments per simulated turn")
    parser.add_argument("--fanout", type=int, default=8, help="concurrent recalls")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--baseline", type=Path, help="a profile to compare against")
    parser.add_argument(
        "--palaces-root",
        type=Path,
        help="where to build throwaway palaces (default: a temp dir, removed on exit)",
    )
    args = parser.parse_args()

    root = args.palaces_root or Path(tempfile.mkdtemp(prefix="eidolon-probe-host-"))
    root.mkdir(parents=True, exist_ok=True)
    model = args.model or "bge-small-zh"
    model_dir = str(Path(args.model_dir).expanduser()) if args.model_dir else ""
    os.environ["EIDOLON_MEMORY_SETTINGS_YAML"] = str(
        _settings_yaml(root, model, args.threads, model_dir)
    )
    os.environ.setdefault("EIDOLON_MEMORY_RUN_DIR", str(root / "run"))

    profile = await _measure(args.fragments, args.fanout, args.rounds)
    profile["probe_args"] = {
        "fragments": args.fragments,
        "fanout": args.fanout,
        "rounds": args.rounds,
        "threads": args.threads,
    }

    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    report(profile, baseline)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
