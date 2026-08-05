"""Chinese retrieval recall on C-MTEB, at a sample size that can rank embedders.

Why this exists: the 49-query fixture cannot separate these models. Its binomial
standard deviation is 3.5 answers, every difference observed between models was
1–3, and McNemar on every pair gave p ≈ 1.00. The honest conclusion was not "the
models are equivalent" but "this measurement cannot tell", and the fix for that is
a bigger query set — not more runs, which is bounded by the same 49 queries.

LongMemEval gave the sample size (500) and a reproducible baseline, but it turned
out to answer a different question. Measured on its documents:

* its session documents run to a median 345 tokens, and **25% of them exceed the
  512-token limit** for a BGE-family model — against 9% for an XLM-R vocabulary,
  because a 21k Chinese vocabulary needs about 30% more tokens for the same English
  text. 512 is the model architecture's ``max_position_embeddings``, not a
  parameter, so a Chinese-specialised model cannot see past it.
* our own fragments run to a median **21 tokens, longest 35, none truncated**.

So a Chinese-specialised model was losing content on a quarter of the documents in
a task whose documents are seventeen times longer than ours. Its score there is a
real property of that task and says very little about this one.

C-MTEB's retrieval tasks avoid both problems at once:

* **1000 queries** per task, so the binomial standard deviation is ~1.5pp rather
  than 3.5 answers — differences of 2–3pp become visible.
* **24–31 character documents**, the same order as our 20 — no truncation for any
  tokenizer, so vocabulary efficiency drops out as a confound.
* Chinese originals, not translations.
* A 100k corpus per task, which is a harder and more realistic ranking problem
  than picking one of 53 candidates.

Each query has exactly one relevant document (``score`` is always 1), so
Recall@k is simply whether that document is in the top k, and MRR@10 is the
reciprocal of its rank.

Self-contained for the same reason ``bench_longmemeval.py`` is: this compares
*models*, so its numbers must be attributable to a model and not to the shape our
adapter had that day. ``tests/memory/test_longmemeval_harness.py`` keeps the shared
model table from drifting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ``manifest`` stays under scripts/benchmark: the service benchmarks there import it
# and moving it would break them for no gain. These suites reach it rather than
# copy it, because provenance recorded two different ways is provenance nobody can
# compare.
_SERVICE_BENCHES = _REPO_ROOT / "scripts" / "benchmark"
if str(_SERVICE_BENCHES) not in sys.path:
    sys.path.insert(0, str(_SERVICE_BENCHES))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_longmemeval import (  # noqa: E402
    MEMPALACE_MODELS,
    MODELS,
    Encoder,
    MemPalaceEncoder,
    provenance,
)

# Overridable so the same file runs from the repo and from a deployment
# directory on the target board, rather than being edited in place there —
# an edited copy is a copy that silently drifts from the one under test.
_DATA = Path(os.environ.get("EIDOLON_BENCH_DATA", _REPO_ROOT / "benchmarks" / "data")) / "cmteb"

#: Ordered by how close their documents are to ours. VideoRetrieval's median is 24
#: characters against our 20; MedicalRetrieval's is 100, which is a different
#: regime and is offered rather than defaulted.
TASKS = ("VideoRetrieval", "EcomRetrieval", "MedicalRetrieval")


def load_task(name: str) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Corpus, queries, and query id → relevant document id."""

    import pyarrow.parquet as pq

    def read(kind: str) -> list[dict]:
        path = _DATA / f"{name}_{kind}.parquet"
        if not path.is_file():
            raise SystemExit(
                f"[FAIL] {path} missing. Download C-MTEB/{name} and "
                f"C-MTEB/{name}-qrels — see the module docstring."
            )
        return pq.read_table(path).to_pylist()

    corpus = read("corpus")
    queries = read("queries")
    qrels = {str(row["qid"]): str(row["pid"]) for row in read("qrels") if row["score"] > 0}
    return corpus, queries, qrels


def evaluate(
    encoder,
    corpus: list[dict],
    queries: list[dict],
    qrels: dict[str, str],
    *,
    ks: tuple[int, ...],
    corpus_limit: int,
    progress_every: int,
    sort_by_length: bool,
) -> dict:
    """Rank the whole corpus for every query, then score by rank of the answer.

    The relevant document is guaranteed to be in the corpus slice even when
    ``--corpus-limit`` shortens it: dropping the answer would measure the slice
    rather than the model, and would flatter every model equally, which is worse
    than a smaller corpus.
    """

    import numpy as np

    scored = [q for q in queries if str(q["id"]) in qrels]
    needed = {qrels[str(q["id"])] for q in scored}

    pool = [d for d in corpus if str(d["id"]) in needed]
    if corpus_limit and corpus_limit < len(corpus):
        have = {str(d["id"]) for d in pool}
        pool += [d for d in corpus if str(d["id"]) not in have][: corpus_limit - len(pool)]
    else:
        pool = corpus

    doc_ids = [str(d["id"]) for d in pool]
    doc_texts = [d.get("text") or "" for d in pool]

    # Sorted by length before batching, then restored. A batch is padded to its
    # longest member, so mixing lengths makes every short document cost what the
    # long one costs: this corpus is 20 tokens at the median and 935 at the maximum,
    # and a random batch of 64 has a longest member around 194 — 7.6x the average.
    # Measured on bge-base: 66ms per document unsorted, against 3.3ms for a single
    # short query. The waste was the harness's, not the model's.
    order = sorted(range(len(doc_texts)), key=lambda i: len(doc_texts[i]))
    started = time.perf_counter()
    sorted_vectors = encoder.embed_documents([doc_texts[i] for i in order])
    embed_seconds = time.perf_counter() - started
    doc_vectors = np.empty((len(order), len(sorted_vectors[0])), dtype=np.float32)
    for slot, original in enumerate(order):
        doc_vectors[original] = sorted_vectors[slot]
    if progress_every:
        print(
            f"  embedded {len(doc_texts)} documents in {embed_seconds:.0f}s "
            f"({embed_seconds * 1000 / len(doc_texts):.1f}ms each)",
            flush=True,
        )

    query_vectors = np.asarray(
        encoder.embed_queries([q.get("text") or "" for q in scored]), dtype=np.float32
    )

    position = {doc_id: i for i, doc_id in enumerate(doc_ids)}
    ranks: list[int] = []
    per_query: list[dict] = []
    top = max(ks + (10,))
    query_started = time.perf_counter()
    for query, vector in zip(scored, query_vectors, strict=True):
        answer = position[qrels[str(query["id"])]]
        scores = doc_vectors @ vector
        # argpartition rather than a full sort: only the head matters and the
        # corpus is 100k rows per query.
        head = np.argpartition(-scores, top)[:top]
        ranked = head[np.argsort(-scores[head])]
        found = np.nonzero(ranked == answer)[0]
        rank = int(found[0]) + 1 if len(found) else 0
        ranks.append(rank)
        per_query.append({"id": str(query["id"]), "rank": rank})

    result = {
        "queries": len(scored),
        "corpus": len(doc_ids),
        "embed_seconds": round(embed_seconds, 1),
        "query_seconds": round(time.perf_counter() - query_started, 1),
        "ms_per_document": round(embed_seconds * 1000 / max(len(doc_texts), 1), 2),
    }
    for k in ks:
        result[f"recall@{k}"] = round(
            sum(1 for r in ranks if 0 < r <= k) / len(ranks), 4
        )
    result["mrr@10"] = round(
        sum(1 / r for r in ranks if 0 < r <= 10) / len(ranks), 4
    )
    result["per_query"] = per_query
    return result


def build(model: str, *, batch_size: int):
    if model in MEMPALACE_MODELS:
        return MemPalaceEncoder(model, batch_size=batch_size)
    return Encoder(model, batch_size=batch_size)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=TASKS, default="VideoRetrieval")
    parser.add_argument("--model", required=True, help="a MODELS or MEMPALACE_MODELS key")
    parser.add_argument(
        "--corpus-limit",
        type=int,
        default=20000,
        help="documents to embed. 0 means the full 100k, which costs 5x this at the "
             "same ranking difficulty for the models we can already separate; the "
             "answer document is always kept",
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--no-sort-by-length",
        action="store_true",
        help="batch in corpus order, which pads every short document to the longest "
             "in its batch — 7.6x the necessary compute on this corpus. Kept so the "
             "effect on R@k can be measured rather than assumed",
    )
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    known = set(MODELS) | set(MEMPALACE_MODELS)
    if args.model not in known:
        print(f"[FAIL] unknown model {args.model!r}; available: {', '.join(sorted(known))}",
              file=sys.stderr)
        return 2

    corpus, queries, qrels = load_task(args.task)
    print(
        f"C-MTEB {args.task} · {len(queries)} queries · corpus {len(corpus)} "
        f"(embedding {args.corpus_limit or len(corpus)}) · model={args.model}",
        flush=True,
    )

    result = evaluate(
        build(args.model, batch_size=args.batch_size),
        corpus,
        queries,
        qrels,
        ks=(1, 5, 10),
        corpus_limit=args.corpus_limit,
        progress_every=args.progress_every,
        sort_by_length=not args.no_sort_by_length,
    )
    result["task"] = args.task
    result["model"] = args.model
    result["provenance"] = provenance(
        suite="cmteb-retrieval",
        command=sys.argv,
        params={
            "task": args.task,
            "model": args.model,
            "corpus_limit": args.corpus_limit,
            "batch_size": args.batch_size,
            "sort_by_length": not args.no_sort_by_length,
        },
    )

    print(
        f"\nR@1 {result['recall@1']:.1%} · R@5 {result['recall@5']:.1%} · "
        f"R@10 {result['recall@10']:.1%} · MRR@10 {result['mrr@10']:.3f}"
    )
    print(
        f"{result['embed_seconds']:.0f}s to embed {result['corpus']} documents "
        f"({result['ms_per_document']:.2f}ms each) · {result['query_seconds']:.0f}s to rank"
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
