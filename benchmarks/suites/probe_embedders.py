"""Score embedder candidates on the real corpus, isolated from everything else.

The quality bench takes twenty minutes and measures retrieval, extraction, the
steward, the graph and the LLM together. Choosing an embedder from it means paying
twenty minutes per candidate and reading the answer through four other subsystems.

So this asks the one question an embedder is answerable for: given the fragments a
real run stored, is the right one in the top 5. It runs in seconds, and the number
moves only when the embedder changes. Resident memory and per-call latency are
measured alongside, because the deployment target is a Raspberry Pi sharing 4 GB
with the rest of Eidolon.

It goes through ``OnnxSentenceEmbedder`` rather than loading models itself, so
what is measured is the code that runs in production — including its pooling and
its prefixes, which are the settings whose being wrong produces no error at all.

    uv run python benchmarks/suites/probe_embedders.py --palace reports/<run>/palaces

Without ``--palace`` it uses the raw corpus turns instead, which is a weaker
document set: retrieval runs against what the steward extracted, not against the
conversation.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "4")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

FIXTURES = REPO_ROOT / "tests/memory/e2e/fixtures"
QUERIES = FIXTURES / "quality_queries.jsonl"
CORPUS = FIXTURES / "companion_corpus.jsonl"

#: MemPalace's own two, measured for comparison. Not candidates: minilm is
#: English-only and embeddinggemma needs 3.1 GB.
MEMPALACE_MODELS = ("embeddinggemma", "minilm")


# Shared, because the obvious one-liner here divided by 1024*1024 unconditionally
# — correct on macOS, where ru_maxrss is bytes, and off by 1024 on Linux, where it
# is kilobytes. This probe's whole point on the Raspberry Pi is the memory column,
# and it would have reported every model as 0 MB.
from hostinfo import rss_mb  # noqa: E402


def documents_from_palace(palace_root: Path) -> list[str]:
    """The fragments the steward actually wrote.

    Retrieval runs against extraction output, so scoring against the raw turns
    would measure a document set that never existed in the index.
    """

    matches = glob.glob(str(palace_root / "**/chroma.sqlite3"), recursive=True)
    if not matches:
        raise SystemExit(f"[FAIL] no chroma.sqlite3 under {palace_root}")
    conn = sqlite3.connect(matches[0])
    try:
        rows = conn.execute(
            "select string_value from embedding_metadata where key='chroma:document'"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def documents_from_corpus() -> list[str]:
    with CORPUS.open(encoding="utf-8") as handle:
        turns = [json.loads(line) for line in handle if line.strip()]
    return [t["user_text"] for t in turns]


def load_queries() -> list[dict]:
    with QUERIES.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class MemPalaceEmbedder:
    """One of MemPalace's own, called the way production calls it.

    Its own shim rather than ``infrastructure.mempalace_embedder``: that one asks
    the ambient configuration which model is current, and this probe compares
    several in one process. The method names match the port so ``score`` below
    does not care which arm it was handed.
    """

    def __init__(self, model: str) -> None:
        from mempalace.embedding import get_embedding_function

        self.label = f"mempalace:{model}"
        self._fn = get_embedding_function(device="cpu", model=model)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._fn(input=texts)

    # Neither of theirs distinguishes the two sides, so this is a property of
    # those models rather than a shortcut.
    embed_queries = embed_documents


def _local(model: str):
    from eidolon.memory.infrastructure.onnx_sentence_embedder import OnnxSentenceEmbedder

    embedder = OnnxSentenceEmbedder(model)
    embedder.label = model  # type: ignore[attr-defined]
    return embedder


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def score(embedder, documents: list[str], queries: list[dict]) -> dict:
    before = rss_mb()
    doc_vectors = embedder.embed_documents(documents)
    after = rss_mb()

    positives = [q for q in queries if not q.get("negative") and q.get("expected_vector_contains")]
    texts = [q["query"] for q in positives]

    started = time.perf_counter()
    query_vectors = embedder.embed_queries(texts)
    batch_ms = (time.perf_counter() - started) * 1000 / max(len(texts), 1)

    single = time.perf_counter()
    embedder.embed_queries(texts[:1])
    single_ms = (time.perf_counter() - single) * 1000

    top1 = top5 = 0
    misses: list[str] = []
    for query, vector in zip(positives, query_vectors, strict=True):
        ranked = sorted(range(len(documents)), key=lambda i: -_dot(doc_vectors[i], vector))
        wanted = query["expected_vector_contains"]

        def hit(indices, wanted=wanted) -> bool:
            joined = " ".join(documents[i] for i in indices)
            return any(w in joined for w in wanted)

        if hit(ranked[:1]):
            top1 += 1
        if hit(ranked[:5]):
            top5 += 1
        else:
            misses.append(f"{query['category']}/{query['id']}: {query['query']}")

    return {
        "model": getattr(embedder, "label", type(embedder).__name__),
        "dim": len(doc_vectors[0]) if doc_vectors else 0,
        "n": len(positives),
        "top1": top1,
        "top5": top5,
        "rss_mb": after,
        "rss_delta_mb": round(after - before, 1),
        "single_ms": round(single_ms, 1),
        "batch_ms": round(batch_ms, 1),
        "misses": misses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--palace",
        type=Path,
        help="palaces root of a completed run; its stored fragments are the documents",
    )
    parser.add_argument(
        "--include-mempalace",
        action="store_true",
        help="also measure minilm and embeddinggemma — the latter loads 3.1 GB",
    )
    parser.add_argument(
        "--models",
        default="",
        help="comma-separated subset to measure; default is every local model. "
             "Nine models means ~2.5 GB of downloads, so name the candidates when "
             "measuring on a small board.",
    )
    parser.add_argument("--out", type=Path, help="write results as JSON")
    args = parser.parse_args()

    from eidolon.memory.domain.embedding_port import LOCAL_EMBEDDING_MODELS

    documents = documents_from_palace(args.palace) if args.palace else documents_from_corpus()
    queries = load_queries()
    source = "stored fragments" if args.palace else "raw corpus turns (weaker)"
    print(f"{len(documents)} documents from {source} · {len(queries)} queries\n")

    # Ours first: MemPalace's embeddinggemma leaves 3 GB resident, and peak RSS is
    # a high-water mark, so anything measured after it reports that instead.
    names = list(LOCAL_EMBEDDING_MODELS)
    if args.models:
        wanted = [m.strip() for m in args.models.split(",") if m.strip()]
        known = set(LOCAL_EMBEDDING_MODELS) | set(MEMPALACE_MODELS)
        unknown = [m for m in wanted if m not in known]
        if unknown:
            raise SystemExit(
                f"[FAIL] unknown model(s): {', '.join(unknown)}\n"
                f"       available: {', '.join(sorted(LOCAL_EMBEDDING_MODELS))}, "
                f"{', '.join(MEMPALACE_MODELS)}"
            )
        names = [m for m in wanted if m in LOCAL_EMBEDDING_MODELS]
    builders = [(name, lambda n=name: _local(n)) for name in names]
    upstream = MEMPALACE_MODELS
    if args.models:
        upstream = tuple(m.strip() for m in args.models.split(",") if m.strip() in MEMPALACE_MODELS)
    if args.include_mempalace or upstream != MEMPALACE_MODELS:
        builders += [(name, lambda n=name: MemPalaceEmbedder(n)) for name in upstream]

    results = []
    for name, build in builders:
        try:
            result = score(build(), documents, queries)
        except Exception as error:  # a candidate that cannot load is a result
            print(f"  !! {name}: {error}")
            continue
        results.append(result)
        print(
            f"{result['model']:26} dim={result['dim']:4}  "
            f"top1={result['top1']:2}/{result['n']}  top5={result['top5']:2}/{result['n']}  "
            f"rss={result['rss_mb']:7.1f}MB (+{result['rss_delta_mb']:5.1f})  "
            f"{result['single_ms']:6.1f}ms/1  {result['batch_ms']:5.1f}ms/batch"
        )

    if results:
        shared = set.intersection(*(set(r["misses"]) for r in results))
        if shared:
            print(
                f"\nmissed by every candidate ({len(shared)}) — these are not embedding\n"
                f"failures; they ask for a time range, the commitments ledger, or an\n"
                f"aggregate over several fragments:"
            )
            for miss in sorted(shared):
                print(f"    {miss}")

    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
