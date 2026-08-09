"""Measure Qwen3-Embedding-0.6B before deciding whether it earns a place.

Kept out of the production adapter on purpose. Its ONNX export needs
``position_ids`` and 56 empty past-key-value tensors, which is a materially
different feed from the BERT-family models we ship. Carrying that for a model we
might reject would be paying for it twice.

Same corpus and queries as benchmarks/suites/probe_embedders.py, so the numbers
are comparable to the four already measured. Last-token pooling, indexed off the
attention mask — a decoder accumulates the sequence at its final token, and a CLS
read would return a vector rather than raise.
"""

from __future__ import annotations

import glob
import json
import os
import resource
import sqlite3
import sys
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "4")

import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from huggingface_hub import hf_hub_download  # noqa: E402
from tokenizers import Tokenizer  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
QUERIES = REPO / "tests/memory/e2e/fixtures/quality_queries.jsonl"
REPO_ID = "onnx-community/Qwen3-Embedding-0.6B-ONNX"

# The documented format: an instruction on the query side, documents unprefixed.
INSTRUCT = (
    "Instruct: Given a question about the user's life, retrieve the memories "
    "that answer it\nQuery: "
)


def rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // (1024 * 1024)


class Qwen3Embedder:
    def __init__(self) -> None:
        model = hf_hub_download(REPO_ID, filename="model_quantized.onnx", subfolder="onnx")
        self.session = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(hf_hub_download(REPO_ID, filename="tokenizer.json"))
        self.tokenizer.enable_padding()
        self.tokenizer.enable_truncation(max_length=512)

        # An empty cache, shaped from the graph's own declaration rather than from
        # the config file — 28 layers × key/value, [batch, heads, 0, head_dim].
        self.past = [i.name for i in self.session.get_inputs() if "past" in i.name]
        shape = next(i.shape for i in self.session.get_inputs() if "past" in i.name)
        self.kv_heads = int(shape[1])
        self.head_dim = int(shape[3])

    def encode(self, texts: list[str], *, batch: int = 8) -> np.ndarray:
        out: list[np.ndarray] = []
        for start in range(0, len(texts), batch):
            chunk = texts[start : start + batch]
            encodings = self.tokenizer.encode_batch(chunk)
            ids = np.array([e.ids for e in encodings], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            # cumsum so a padded row's positions still start at 0 on its first
            # real token; plain arange would offset left-padded rows.
            position_ids = np.maximum(np.cumsum(mask, axis=1) - 1, 0).astype(np.int64)

            feed = {
                "input_ids": ids,
                "attention_mask": mask,
                "position_ids": position_ids,
            }
            empty = np.zeros((ids.shape[0], self.kv_heads, 0, self.head_dim), dtype=np.float32)
            for name in self.past:
                feed[name] = empty

            hidden = self.session.run(["last_hidden_state"], feed)[0]
            last = mask.shape[1] - 1 - np.argmax(mask[:, ::-1], axis=1)
            pooled = hidden[np.arange(hidden.shape[0]), last]
            pooled = pooled / (np.linalg.norm(pooled, axis=1, keepdims=True) + 1e-12)
            out.append(pooled)
        return np.concatenate(out, axis=0)


def documents(palace_root: Path) -> list[str]:
    (db,) = glob.glob(str(palace_root / "*/chroma.sqlite3"))
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "select string_value from embedding_metadata where key='chroma:document'"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def main() -> None:
    palace = Path(sys.argv[1])
    docs = documents(palace)
    with QUERIES.open(encoding="utf-8") as handle:
        queries = [json.loads(line) for line in handle if line.strip()]
    positives = [q for q in queries if not q.get("negative") and q.get("expected_vector_contains")]

    before = rss_mb()
    embedder = Qwen3Embedder()
    doc_vectors = embedder.encode(docs)
    print(
        f"{len(docs)} documents · {len(positives)} answerable queries · dim={doc_vectors.shape[1]}"
    )
    print(f"RSS after documents: {rss_mb()} MB (+{rss_mb() - before})")

    for label, prefix in (("no instruction", ""), ("with instruction", INSTRUCT)):
        texts = [prefix + q["query"] for q in positives]
        started = time.perf_counter()
        query_vectors = embedder.encode(texts)
        batch_ms = (time.perf_counter() - started) * 1000 / len(texts)

        single = time.perf_counter()
        embedder.encode(texts[:1])
        single_ms = (time.perf_counter() - single) * 1000

        top1 = top5 = 0
        misses = []
        for query, vector in zip(positives, query_vectors, strict=True):
            ranked = np.argsort(-(doc_vectors @ vector))
            wanted = query["expected_vector_contains"]

            def hit(indices, wanted=wanted):
                joined = " ".join(docs[i] for i in indices)
                return any(w in joined for w in wanted)

            top1 += hit(ranked[:1])
            if hit(ranked[:5]):
                top5 += 1
            else:
                misses.append(f"{query['category']}/{query['id']}")

        print(
            f"  {label:18} top1={top1:2}/{len(positives)}  top5={top5:2}/{len(positives)}  "
            f"{single_ms:6.1f}ms/1  {batch_ms:5.1f}ms/batch  peak RSS {rss_mb()} MB"
        )


if __name__ == "__main__":
    main()
