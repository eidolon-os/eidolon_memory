"""Retrieval over multi-turn dialogue memory — LoCoMo and CLongEval.

This is the task the service actually performs: a person asks something about their
own past, and the right turn has to come back. C-MTEB gave 1000 Chinese queries at
the right document length, but its domain is video titles and product listings. These
two are conversations.

**LoCoMo** (Snap Research, ``locomo10.json``) — 10 conversations, 1986 questions,
English. Each question carries ``evidence`` naming the turns that answer it as
``"D1:3"``, and every turn carries a matching ``dia_id``. So the relevance labels are
the dataset's own; nothing is inferred.

**CLongEval 1-2 long conversation memory** (CUHK, ``small.jsonl``) — 358 questions,
Chinese, companion-style dialogue with dated recall ("4月27日，我和你推荐过一本书，
书名是什么？"). The closest domain match to our own fixture that exists at this size.

The two are scored the same way but their labels are not equally strong, and the
difference is stated in the output rather than smoothed over:

* LoCoMo's ``evidence`` is **annotated**. A hit means the labelled turn is in the
  top k.
* CLongEval gives an ``answer`` string and no evidence pointer, so the evidence turn
  is **derived here** by finding which turn contains the answer text. That is a
  reasonable derivation and it is still a derivation: a question whose answer text
  appears in several turns, or in none verbatim, is labelled by this code rather than
  by the authors. Both counts are reported (``derived_labels``,
  ``questions_unlabelled``) so a reader can see how much of the score rests on it.

Deliberately **not** used: CLongEval ``4-1_key_passage_retrieval``. Its queries are
32-character random strings ("4JJ8T0BlIKNuQzcyEKYZvZnFQvLsUWzu") used as literal keys
into the context. That measures whether a long-context model can find a needle, and a
semantic embedder has nothing to rank by — a random string has no neighbours. Running
it would produce a near-chance number that could easily be read as "this model is
weak in Chinese".

Self-contained, like the other model-evaluation suites: it imports the encoder from
``bench_longmemeval`` and nothing from ``eidolon.memory``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_REPO_ROOT = _HERE.parents[1]
_SERVICE_BENCHES = _REPO_ROOT / "scripts" / "benchmark"
for extra in (str(_REPO_ROOT), str(_SERVICE_BENCHES)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

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
_DATA = Path(os.environ.get("EIDOLON_BENCH_DATA", _REPO_ROOT / "benchmarks" / "data"))


# ── LoCoMo: labels come from the dataset ─────────────────────────────────────


def load_locomo(path: Path) -> list[dict]:
    """One item per question: the turns of its conversation and the labelled ones.

    ``evidence`` entries look like ``"D1:3"`` and turns carry ``dia_id``. Questions
    whose evidence names no turn present in the conversation are dropped and counted
    rather than scored as misses — a missing label is not a retrieval failure.
    """

    conversations = json.loads(path.read_text(encoding="utf-8"))
    items: list[dict] = []
    for conversation in conversations:
        conv = conversation.get("conversation") or {}
        turns: list[dict] = []
        for key in sorted(k for k in conv if re.fullmatch(r"session_\d+", k)):
            for turn in conv[key] or []:
                text = (turn.get("text") or "").strip()
                if text:
                    turns.append(
                        {
                            "id": str(turn.get("dia_id") or ""),
                            "text": f"{turn.get('speaker', '')}: {text}",
                        }
                    )
        by_id = {t["id"] for t in turns}
        for question in conversation.get("qa") or []:
            evidence = [str(e) for e in (question.get("evidence") or [])]
            labelled = [e for e in evidence if e in by_id]
            if not labelled:
                continue
            items.append(
                {
                    "query": question.get("question") or "",
                    "documents": turns,
                    "answers": labelled,
                    "category": str(question.get("category", "")),
                    "label_source": "annotated",
                }
            )
    return items


# ── CLongEval: labels derived from the answer text ───────────────────────────

_TURN_SPLIT = re.compile(r"\n(?=(?:用户|AI)[:：])")


def load_clongeval(path: Path, *, limit: int = 0) -> tuple[list[dict], int]:
    """One item per question. Returns (items, questions_with_no_locatable_answer).

    The context is a transcript with ``用户:`` / ``AI:`` turns; it is split on those
    markers. The evidence turn is whichever turn contains the answer string, which is
    a derivation rather than an annotation — see the module docstring.
    """

    items: list[dict] = []
    unlabelled = 0
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if limit and index >= limit:
                break
            row = json.loads(line)
            context = row.get("context") or ""
            answer = (row.get("answer") or "").strip()
            turns = [
                {"id": str(i), "text": part.strip().strip("“”\"")}
                for i, part in enumerate(_TURN_SPLIT.split(context))
                if part.strip()
            ]
            # The answer is often a phrase inside a longer turn; a bare substring test
            # is the honest rule and it is stated as such. Punctuation is stripped
            # because the answer field quotes titles ("《小王子》") that appear
            # unquoted in the transcript.
            needle = answer.strip("《》“”\"。 ")
            labelled = [t["id"] for t in turns if needle and needle in t["text"]]
            if not labelled:
                unlabelled += 1
                continue
            items.append(
                {
                    "query": row.get("query") or "",
                    "documents": turns,
                    "answers": labelled,
                    "category": "",
                    "label_source": "derived",
                }
            )
    return items, unlabelled


# ── scoring ───────────────────────────────────────────────────────────────────


def evaluate(encoder, items: list[dict], *, ks: tuple[int, ...]) -> dict:
    """Rank each question's own turns. Recall@k, plus MRR@10.

    The corpus is per question, as LoCoMo's own evaluation does it: the candidate set
    is that conversation's turns, not a global pool. A hit is any labelled turn in the
    top k — ``recall_any``, the measure MemPalace publishes.
    """

    import numpy as np

    ranks: list[int] = []
    per_question: list[dict] = []
    embed_seconds = 0.0
    documents_embedded = 0

    # Conversations repeat across their questions, so each is embedded once.
    cache: dict[int, tuple[np.ndarray, list[str]]] = {}

    for item in items:
        key = id(item["documents"])
        if key not in cache:
            texts = [d["text"] for d in item["documents"]]
            order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
            started = time.perf_counter()
            ordered = encoder.embed_documents([texts[i] for i in order])
            embed_seconds += time.perf_counter() - started
            documents_embedded += len(texts)
            vectors = np.empty((len(order), len(ordered[0])), dtype=np.float32)
            for slot, original in enumerate(order):
                vectors[original] = ordered[slot]
            cache[key] = (vectors, [d["id"] for d in item["documents"]])
        vectors, ids = cache[key]

        (query_vector,) = encoder.embed_queries([item["query"]])
        scores = vectors @ np.asarray(query_vector, dtype=np.float32)
        wanted = set(item["answers"])
        top = max(ks + (10,))
        head = np.argpartition(-scores, min(top, len(scores) - 1))[:top]
        ranked = head[np.argsort(-scores[head])]
        rank = next((i + 1 for i, idx in enumerate(ranked) if ids[idx] in wanted), 0)
        ranks.append(rank)
        per_question.append(
            {"rank": rank, "candidates": len(ids), "label_source": item["label_source"]}
        )

    result = {
        "questions": len(ranks),
        "candidates_median": statistics.median(p["candidates"] for p in per_question),
        "documents_embedded": documents_embedded,
        "embed_seconds": round(embed_seconds, 1),
        "ms_per_document": round(embed_seconds * 1000 / max(documents_embedded, 1), 2),
        "derived_labels": sum(1 for p in per_question if p["label_source"] == "derived"),
    }
    for k in ks:
        result[f"recall@{k}"] = round(sum(1 for r in ranks if 0 < r <= k) / len(ranks), 4)
    result["mrr@10"] = round(sum(1 / r for r in ranks if 0 < r <= 10) / len(ranks), 4)
    result["per_question"] = per_question
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("locomo", "clongeval"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=0, help="first N questions")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    known = set(MODELS) | set(MEMPALACE_MODELS)
    if args.model not in known:
        print(f"[FAIL] unknown model {args.model!r}; have: {', '.join(sorted(known))}",
              file=sys.stderr)
        return 2

    unlabelled = 0
    if args.dataset == "locomo":
        path = _DATA / "locomo" / "locomo10.json"
        if not path.is_file():
            print(f"[FAIL] {path} missing — fetch data/locomo10.json from "
                  f"github.com/snap-research/locomo", file=sys.stderr)
            return 2
        items = load_locomo(path)
        language = "en"
    else:
        path = _DATA / "clongeval" / "1-2_long_conversation_memory_small.jsonl"
        if not path.is_file():
            print(f"[FAIL] {path} missing — fetch 1-2_long_conversation_memory/small.jsonl "
                  f"from huggingface.co/datasets/zexuanqiu22/CLongEval", file=sys.stderr)
            return 2
        items, unlabelled = load_clongeval(path)
        language = "zh"

    if args.limit:
        items = items[: args.limit]

    encoder = (
        MemPalaceEncoder(args.model, batch_size=args.batch_size)
        if args.model in MEMPALACE_MODELS
        else Encoder(args.model, batch_size=args.batch_size)
    )
    print(
        f"{args.dataset} ({language}) · {len(items)} labelled questions"
        + (f" · {unlabelled} unlabelled and skipped" if unlabelled else "")
        + f" · model={args.model}",
        flush=True,
    )

    result = evaluate(encoder, items, ks=(1, 5, 10))
    result["dataset"] = args.dataset
    result["language"] = language
    result["model"] = args.model
    result["questions_unlabelled"] = unlabelled
    result["label_source"] = "annotated" if args.dataset == "locomo" else "derived"
    result["provenance"] = provenance(
        suite=f"dialogue-memory-{args.dataset}",
        command=sys.argv,
        params={
            "dataset": args.dataset,
            "model": args.model,
            "limit": args.limit,
            "batch_size": args.batch_size,
        },
    )

    print(
        f"\nR@1 {result['recall@1']:.1%} · R@5 {result['recall@5']:.1%} · "
        f"R@10 {result['recall@10']:.1%} · MRR@10 {result['mrr@10']:.3f}"
    )
    print(
        f"{result['questions']} questions over a median {result['candidates_median']:.0f} "
        f"candidate turns · {result['embed_seconds']:.0f}s embedding "
        f"({result['ms_per_document']:.2f}ms/turn)"
    )
    if result["label_source"] == "derived":
        print(
            f"labels derived from answer text, not annotated: {result['derived_labels']} "
            f"scored, {unlabelled} skipped for having no locatable answer"
        )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
