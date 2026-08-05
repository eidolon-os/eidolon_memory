"""LongMemEval retrieval recall, scored by MemPalace's own harness.

MemPalace publishes 96.6% R@5 on LongMemEval's 500 questions and 98.4% on the
450-question held-out split. Those are the numbers to compare against, and a
comparison is only worth making under the same definition of a hit — so this
imports their harness and calls their ``evaluate_retrieval`` rather than
reimplementing it. Alignment by construction, not by assertion.

What their measure actually is, read from their code rather than assumed:

* The corpus is **per question**. Each entry carries its own ``haystack_sessions``
  — around 53 of them — and a fresh collection is built for that question alone.
  So the task is "is the labelled session in the top 5 of these ~53", not
  "…of a global index".
* A session document is the **user turns only**, joined by newlines. Assistant
  text is dropped (``build_palace_and_retrieve``, granularity="session").
* ``recall_any`` is the headline R@5: at least one labelled session in the top k.
  ``recall_all`` requires all of them. Their published figure is recall_any.
* No LLM runs at ingestion. Their baseline stores sessions verbatim.

Two arms:

``--arm upstream`` reproduces their baseline on this machine with the embedder
they used (all-MiniLM-L6-v2, ChromaDB's default). **Run this first.** It is the
harness self-check: if it does not land near 96.6%, the wiring here is wrong and
nothing else measured with it means anything.

``--arm ours`` keeps their corpus construction and their scoring and swaps in one
of the encoders below, ranking by cosine over normalised vectors. It answers the
question our own 49-query fixture provably cannot: at n=500 a two-answer difference
is detectable, where at n=49 the binomial standard deviation is 3.5 answers and
every model we measured sat inside it.

This arm does **not** run our steward, our recall pipeline, or our graph. It
isolates the embedder against a large query set, which is the one thing missing
from the evidence. An end-to-end LongMemEval run would need an LLM extraction per
session — measured at ~30 s per turn, so days — and is a separate exercise.

**Deliberately self-contained.** The encoder here is this script's own, not
``OnnxSentenceEmbedder``, and the model table is duplicated below rather than
imported from ``domain/embedding_port.py``. That is the opposite of the choice made
in ``probe_embedders.py``, and for a reason: that probe asks whether *our deployed
path* retrieves well and must therefore run the shipping code, pooling and prefixes
included. This one asks whether a *model* retrieves well, against another project's
published figures, so its numbers have to be attributable to the model rather than
to whatever shape the adapter had that day. A cross-project comparison that moves
when we rename a method is not a comparison.

The cost of the duplication is that a pooling or prefix rule can drift between the
two. That is what ``tests/memory/test_longmemeval_harness.py`` is for: it asserts
the tables agree, so drift fails a test instead of quietly changing a published
number.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
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

_UPSTREAM = _REPO_ROOT / "benchmarks" / "upstream"
if str(_UPSTREAM) not in sys.path:
    sys.path.insert(0, str(_UPSTREAM))

_DEFAULT_DATA = _REPO_ROOT / "benchmarks" / "data" / "longmemeval_s_cleaned.json"
_DEFAULT_SPLIT = _UPSTREAM / "lme_split_50_450.json"


def _upstream():
    """Their harness. Imported lazily so ``--help`` does not open a Chroma client."""

    import longmemeval_bench

    return longmemeval_bench


# ── this script's own encoder ─────────────────────────────────────────────────
#
# Duplicated from domain/embedding_port.py on purpose (see the module docstring).
# ``pooling`` and the prefixes are the fields whose being wrong raises nothing and
# only ranks worse, so they are the ones the drift test checks.

MODELS: dict[str, dict] = {
    "bge-small-zh": {"repo": "Xenova/bge-small-zh-v1.5", "pooling": "cls"},
    "bge-base-zh": {"repo": "Xenova/bge-base-zh-v1.5", "pooling": "cls"},
    "bge-large-zh": {"repo": "Xenova/bge-large-zh-v1.5", "pooling": "cls"},
    "bge-m3": {"repo": "onnx-community/bge-m3-ONNX", "pooling": "cls"},
    "gte-multilingual-base": {
        "repo": "onnx-community/gte-multilingual-base",
        "pooling": "cls",
    },
    "multilingual-e5-small": {
        "repo": "Xenova/multilingual-e5-small",
        "pooling": "mean",
        "query_prefix": "query: ",
        "document_prefix": "passage: ",
    },
    "multilingual-e5-base": {
        "repo": "Xenova/multilingual-e5-base",
        "pooling": "mean",
        "query_prefix": "query: ",
        "document_prefix": "passage: ",
    },
    "multilingual-e5-large": {
        "repo": "Xenova/multilingual-e5-large",
        "pooling": "mean",
        "query_prefix": "query: ",
        "document_prefix": "passage: ",
    },
    "qwen3-embedding-0.6b": {
        "repo": "onnx-community/Qwen3-Embedding-0.6B-ONNX",
        "pooling": "last",
        "query_prefix": (
            "Instruct: Given a question about the user's life, retrieve the "
            "memories that answer it\nQuery: "
        ),
    },
}

#: Entries above that production deliberately does not ship.
#:
#: qwen3 was removed from ``LOCAL_EMBEDDING_MODELS`` on cost — 1 GB resident and
#: 45 ms per query against a recall path whose end-to-end p95 is 20 ms — and the
#: decoder handling it needed went with it: last-token pooling, a ``position_ids``
#: feed and an empty key-value cache, none of which any remaining model declares.
#:
#: It stays runnable *here* because this file has its own encoder with its own
#: decoder feed, and because a rejection resting on a number nobody can re-measure
#: is a rejection nobody can check. 22.5 s per LongMemEval question is the figure
#: the results report cites; this is where it comes from.
REJECTED_MODELS = ("qwen3-embedding-0.6b",)

#: MemPalace's own two, reachable through their factory rather than the table
#: above. Measured here because their published 96.6% is minilm's, and
#: embeddinggemma is the only model that scored above the pack on our Chinese
#: end-to-end run — so what it does at n=500 is the open question.
#:
#: Importing ``mempalace.embedding`` is not the coupling this file avoids: it is a
#: pinned third-party library, the same class of dependency as their harness. What
#: is avoided is importing *our* code, which is what moves.
MEMPALACE_MODELS = ("minilm", "embeddinggemma")

_MAX_TOKENS = 512


class MemPalaceEncoder:
    """One of MemPalace's own encoders, with the method names ``run`` expects.

    Neither of theirs distinguishes query from document, so the two methods being
    the same call is a property of those models rather than a shortcut here.
    """

    def __init__(self, model: str, *, batch_size: int = 32) -> None:
        from mempalace.embedding import get_embedding_function

        self._fn = get_embedding_function(device="cpu", model=model)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._fn(input=texts)

    embed_queries = embed_documents


class Encoder:
    """One quantized ONNX encoder, fed according to what its graph declares."""

    def __init__(self, model: str, *, batch_size: int = 32) -> None:
        import numpy as np
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        spec = MODELS[model]
        self.spec = spec
        self.np = np
        self.batch_size = batch_size
        weights = hf_hub_download(spec["repo"], filename="model_quantized.onnx", subfolder="onnx")
        self.session = ort.InferenceSession(weights, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.past = tuple(i.name for i in self.session.get_inputs() if "past" in i.name)
        self.past_shape = None
        if self.past:
            declared = next(i.shape for i in self.session.get_inputs() if i.name == self.past[0])
            self.past_shape = (int(declared[1]), int(declared[3]))
        self.tokenizer = Tokenizer.from_file(
            hf_hub_download(spec["repo"], filename="tokenizer.json")
        )
        self.tokenizer.enable_padding()
        self.tokenizer.enable_truncation(max_length=_MAX_TOKENS)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        np = self.np
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = texts[start : start + self.batch_size]
            encodings = self.tokenizer.encode_batch(chunk)
            ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            if "position_ids" in self.inputs:
                feed["position_ids"] = np.maximum(np.cumsum(mask, axis=1) - 1, 0).astype(np.int64)
            if self.past and self.past_shape:
                heads, head_dim = self.past_shape
                empty = np.zeros((ids.shape[0], heads, 0, head_dim), dtype=np.float32)
                for name in self.past:
                    feed[name] = empty

            wanted = ["last_hidden_state"] if self.past else None
            hidden = self.session.run(wanted, feed)[0]

            pooling = self.spec["pooling"]
            if pooling == "cls":
                pooled = hidden[:, 0]
            elif pooling == "last":
                last = mask.shape[1] - 1 - np.argmax(mask[:, ::-1], axis=1)
                pooled = hidden[np.arange(hidden.shape[0]), last]
            else:
                weights = mask[..., None].astype(np.float32)
                pooled = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)

            norms = np.linalg.norm(pooled, axis=1, keepdims=True) + 1e-12
            out.extend((pooled / norms).tolist())
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode([self.spec.get("document_prefix", "") + t for t in texts])

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._encode([self.spec.get("query_prefix", "") + t for t in texts])


def provenance(*, suite: str, command: list[str], params: dict) -> dict:
    """Commit, machine, and every parameter that can move the number.

    Reuses ``manifest.code_provenance`` / ``machine_facts`` — the repo already had
    them and both benchmarks here were writing bare aggregates without them, which
    is how several figures in this investigation became hard to attribute after the
    fact. ``storage_facts`` is not used: it describes a palace, and these suites
    have none.

    ``params`` matters as much as the commit. ``batch_size`` changes padding, which
    changes the order of a float summation, so two runs of the same script can
    differ slightly — and a result that does not record it cannot be reproduced or
    compared. That is the gap that made "does length-sorted batching change R@k?"
    unanswerable from the files already on disk.
    """

    from manifest import code_provenance, machine_facts, utc_stamp

    return {
        "suite": suite,
        "started_at": utc_stamp(),
        "code": code_provenance(_REPO_ROOT),
        "machine": machine_facts(),
        "command": " ".join(command),
        "params": params,
    }


def session_documents(entry: dict) -> tuple[list[str], list[str]]:
    """One document per session, user turns only — their session granularity.

    Deliberately duplicated from their ``build_palace_and_retrieve`` rather than
    called: that function also creates a Chroma collection and embeds with their
    embedding function, which is exactly what the ``ours`` arm replaces. The rule
    it encodes — join the user turns, skip a session that has none — is copied
    here verbatim, and the ``upstream`` arm calls their function so any drift
    shows up as the two arms disagreeing.
    """

    documents: list[str] = []
    ids: list[str] = []
    for session, session_id in zip(
        entry["haystack_sessions"], entry["haystack_session_ids"], strict=True
    ):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        if user_turns:
            documents.append("\n".join(user_turns))
            ids.append(session_id)
    return documents, ids


def rank_with_our_embedder(
    entry: dict, embedder, *, batch_size: int
) -> tuple[list[int], list[str]]:
    """Rank this question's sessions by cosine, using our port.

    Vectors are L2-normalised by the adapter, so a dot product is the cosine and
    ranking by it is the same ordering Chroma would produce for its cosine space.
    """

    documents, ids = session_documents(entry)
    if not documents:
        return [], []

    doc_vectors = embedder.embed_documents(documents)
    (query_vector,) = embedder.embed_queries([entry["question"]])

    scores = [
        sum(a * b for a, b in zip(vector, query_vector, strict=True)) for vector in doc_vectors
    ]
    return sorted(range(len(documents)), key=lambda i: -scores[i]), ids


def run(
    entries: list[dict],
    *,
    arm: str,
    model: str,
    k: int,
    batch_size: int,
    progress_every: int,
) -> dict:
    upstream = _upstream()
    embedder = None
    if arm == "ours":
        embedder = (
            MemPalaceEncoder(model, batch_size=batch_size)
            if model in MEMPALACE_MODELS
            else Encoder(model, batch_size=batch_size)
        )
    else:
        # Their global, set the way their CLI sets it before a run.
        upstream._bench_embed_fn = upstream._make_embed_fn(model)

    per_type: dict[str, list[float]] = defaultdict(list)
    per_question: list[dict] = []
    recall_any: list[float] = []
    recall_all: list[float] = []
    ndcg: list[float] = []
    latencies: list[float] = []
    started = time.perf_counter()

    for index, entry in enumerate(entries, 1):
        correct = set(entry["answer_session_ids"])
        turn = time.perf_counter()
        if arm == "ours":
            rankings, ids = rank_with_our_embedder(entry, embedder, batch_size=batch_size)
        else:
            rankings, _docs, ids, _dates = upstream.build_palace_and_retrieve(
                entry, granularity="session", n_results=max(k, 50)
            )
        latencies.append((time.perf_counter() - turn) * 1000)

        if not ids:
            recall_any.append(0.0)
            recall_all.append(0.0)
            ndcg.append(0.0)
            per_type[entry["question_type"]].append(0.0)
            continue

        any_hit, all_hit, ndcg_score = upstream.evaluate_retrieval(rankings, correct, ids, k)
        # Per question, so the aggregate can be audited and two runs can be
        # compared query by query. MemPalace commits this for the same reason:
        # "you can inspect every individual answer — not just the aggregate."
        # Without it the flip analysis that showed this metric's stability on one
        # model and its instability on another would not have been possible.
        per_question.append(
            {
                "id": entry["question_id"],
                "type": entry["question_type"],
                "recall_any": any_hit,
                "recall_all": all_hit,
                "ndcg": round(ndcg_score, 4),
                "rank_of_first_hit": next(
                    (i + 1 for i, idx in enumerate(rankings) if ids[idx] in correct), 0
                ),
            }
        )
        recall_any.append(any_hit)
        recall_all.append(all_hit)
        ndcg.append(ndcg_score)
        per_type[entry["question_type"]].append(any_hit)

        if progress_every and index % progress_every == 0:
            print(
                f"  {index}/{len(entries)}  R@{k}={statistics.mean(recall_any):.3f}  "
                f"({(time.perf_counter() - started) / index:.2f}s/q)",
                flush=True,
            )

    return {
        "arm": arm,
        "model": model,
        "k": k,
        "questions": len(entries),
        f"recall_any@{k}": round(statistics.mean(recall_any), 4),
        f"recall_all@{k}": round(statistics.mean(recall_all), 4),
        "ndcg@10": round(statistics.mean(ndcg), 4),
        "seconds": round(time.perf_counter() - started, 1),
        "ms_per_question_p50": round(statistics.median(latencies), 1),
        "by_question_type": {
            name: {"n": len(values), f"recall_any@{k}": round(statistics.mean(values), 4)}
            for name, values in sorted(per_type.items())
        },
        "per_question": per_question,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=_DEFAULT_DATA)
    parser.add_argument("--split-file", type=Path, default=_DEFAULT_SPLIT)
    parser.add_argument(
        "--split",
        choices=("dev", "held_out", "all"),
        default="dev",
        help="dev is the 50 questions they tuned on and is safe to iterate against; "
        "held_out is the 450 their publishable 98.4%% comes from — touch it once",
    )
    parser.add_argument(
        "--arm",
        choices=("upstream", "ours"),
        default="upstream",
        help="upstream reproduces their baseline here as a harness self-check; "
        "ours swaps the embedder and keeps everything else",
    )
    parser.add_argument(
        "--model",
        default="",
        help="upstream: 'default' is ChromaDB's all-MiniLM-L6-v2, theirs. "
        "ours: a key of MODELS in this file",
    )
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0, help="first N of the split, for smoke runs")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.data.is_file():
        print(
            f"[FAIL] dataset not found: {args.data}\n"
            f"       Download longmemeval_s_cleaned.json from "
            f"huggingface.co/datasets/xiaowu0162/longmemeval-cleaned — the same file "
            f"MemPalace runs, and the deprecated original would not be comparable.",
            file=sys.stderr,
        )
        return 2

    # "default" is their own name for ChromaDB's built-in all-MiniLM-L6-v2, which
    # is the embedder their 96.6% was measured with. Passing "minilm" also lands
    # there, but only because it is absent from their MODEL_MAP and their fastembed
    # path then fails to an unannounced fallback — right by accident is not the
    # same as right.
    model = args.model or ("default" if args.arm == "upstream" else "bge-small-zh")
    entries = json.loads(args.data.read_text(encoding="utf-8"))
    by_id = {e["question_id"]: e for e in entries}

    if args.split != "all":
        split = json.loads(args.split_file.read_text(encoding="utf-8"))
        wanted = split[args.split]
        missing = [q for q in wanted if q not in by_id]
        if missing:
            print(
                f"[FAIL] {len(missing)} question ids in the {args.split} split are not in "
                f"{args.data.name}; the split and the dataset do not match.",
                file=sys.stderr,
            )
            return 2
        entries = [by_id[q] for q in wanted]
    if args.limit:
        entries = entries[: args.limit]

    print(
        f"LongMemEval · {args.split} · {len(entries)} questions · arm={args.arm} "
        f"model={model} · k={args.k}",
        flush=True,
    )
    result = run(
        entries,
        arm=args.arm,
        model=model,
        k=args.k,
        batch_size=args.batch_size,
        progress_every=args.progress_every,
    )
    result["split"] = args.split
    result["provenance"] = provenance(
        suite="longmemeval",
        command=sys.argv,
        params={
            "arm": args.arm,
            "model": model,
            "split": args.split,
            "k": args.k,
            "batch_size": args.batch_size,
            "limit": args.limit,
            "max_tokens": _MAX_TOKENS,
            "length_sorted_batching": False,
        },
    )

    print(f"\nR@{args.k} (recall_any) = {result[f'recall_any@{args.k}']:.1%}")
    print(f"R@{args.k} (recall_all) = {result[f'recall_all@{args.k}']:.1%}")
    print(f"NDCG@10          = {result['ndcg@10']:.3f}")
    print(f"{result['seconds']:.0f}s total · {result['ms_per_question_p50']:.0f}ms/question p50")
    print("\nby question type:")
    for name, stats in result["by_question_type"].items():
        print(f"  {name:28} {stats[f'recall_any@{args.k}']:.1%}  (n={stats['n']})")

    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
