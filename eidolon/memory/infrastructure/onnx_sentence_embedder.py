"""Quantized ONNX sentence encoders, chosen for Chinese on a small machine.

One implementation of ``EmbeddingPort``, and the default one. It knows about ONNX
Runtime, tokenizers, pooling and where its weights live; it knows nothing about
Chroma — that shape is in ``chroma_embedding_function`` and wraps this.

Why this exists: MemPalace ships two embedders and neither is deployable for us.
``minilm`` is English-only — 5/43 top-1 on our Chinese corpus against 21–27 for
everything else, which is a wrong tool rather than a tuning gap.
``embeddinggemma`` retrieves best of everything measured but costs 3 GB resident
and 45 ms per call, and the deployment target is a Raspberry Pi sharing 4 GB with
the rest of Eidolon.

**Every latency figure below is from a 12-core Apple Silicon machine at
``OMP_NUM_THREADS=4``.** The deployment target is a Raspberry Pi 5 — four Cortex-A76
cores at 2.4 GHz — so the thread count already matches but the per-core speed does
not. The ordering between models transfers and the resident memory transfers almost
exactly; the absolute milliseconds do not, and have to be re-measured on the board
with ``benchmarks/suites/probe_embedders.py``. That is also why the cheapest model
matters more here than the table alone suggests: it is the only one with headroom
for a slower core.

Measured twice — the palaces of two full runs, holding 35 and 36 stored fragments,
against the same 49 real queries (``benchmarks/suites/probe_embedders.py``, so the
numbers can be reproduced and argued with):

| model                 | dim  | top-1       | top-5       |     RSS | ms/call |
|-----------------------|------|-------------|-------------|---------|---------|
| bge-small-zh          |  512 | 27 → 24 /43 | 36 → 35 /43 |  130 MB |     1.0 |
| bge-base-zh           |  768 | 26 → 24 /43 | 39 → 36 /43 |  130 MB |     3.0 |
| bge-large-zh          | 1024 | 24 → 26 /43 | 38 → 36 /43 |  485 MB |     8.6 |
| multilingual-e5-small |  384 | 21 → 24 /43 | 35 → 35 /43 |  180 MB |     1.5 |
| embeddinggemma        |  384 |       25/43 |       37/43 |    3 GB |    44.4 |
| minilm                |  384 |        5/43 |       16/43 |  380 MB |    17.5 |

The arrows are the *same model* on two corpora of 35 and 36 stored fragments,
answering the same 49 queries. **A model swings ±3 between them, which is larger
than the gap between the three BGE sizes.** On the first corpus bge-small has the
best top-1 and bge-large the worst; on the second that inverts. So at this sample
size top-1 and top-5 cannot rank them, and an earlier version of this comment was
wrong to say bge-base had the best top-1 or that bigger was not better — both were
readings of noise.

What the same two runs *do* establish, because it is stable to within a few MB and
a fraction of a millisecond: cost. bge-large is ~3.7× the memory of small and ~8×
the latency for no measurable retrieval gain. And minilm's 5/43 and
embeddinggemma's 37/43 sit far outside the ±3 band, so those two differences are
real: minilm is the wrong tool for Chinese, and embeddinggemma genuinely retrieves
best.

**So the default is chosen on cost, not on score**: among models we cannot
distinguish, take the cheapest. That is bge-small-zh — 130 MB and under a
millisecond, against a target of a Raspberry Pi sharing 4 GB with the rest of
Eidolon. embeddinggemma is excluded by its 3 GB, not by its quality.

**Qwen3-Embedding-0.6B is a candidate where 1 GB of memory is available.** It is a
decoder embedder — last-token pooling, instruction-aware queries, and an export
that declares ``position_ids`` and a 56-tensor key-value cache — so the feed here
is driven by the session's declared inputs rather than by the model family.

Measured on this machine, 20 single-query calls plus the 40-turn corpus:

| | bge-small-zh | qwen3-0.6b | ratio |
|---|---|---|---|
| resident | +110 MB | **+1014 MB** | 9.2× |
| query p50 | 0.9 ms | **45.3 ms** | 50× |
| query p95 | 1.3 ms | **51.6 ms** | 40× |
| 40 documents | 38.3 ms | 860 ms | 22× |

The query figure is the one that decides deployment: it lands inside the recall
path, whose end-to-end p95 is 20 ms on bge-small. Ingest throughput barely matters
by comparison — 860 ms of embedding sits behind an LLM steward taking ~30 s per
turn.

Its retrieval on 43 queries is 35–40 top-5, the same band as everything else. That
tells us less than it seems to: four models run end to end showed the probe's top-5
does not predict the pipeline's answer rate — the highest-scoring probe model
(multilingual-e5-large, 41/40) finished level with the lowest. Only an end-to-end
run ranks an embedder here, and that metric was measured at **zero** variance across
two identical runs.

Separating any of these would need several hundred queries rather than 43, which
is one concrete reason to run the public suites.

Two queries — "我跟客户吵架了" and "我最近工作压力大吗" — are missed by all five,
including the 3 GB one. Four more ("我以后想做什么", "我计划去哪里",
"我什么时候开心", "上周聊了什么") are missed by every deployable candidate. None of
these are embedding failures: they ask for a time range, the commitments ledger,
or an aggregate over several fragments. No embedder answers them, so no embedder
choice fixes them; see docs/TEST_REPORT.md for that work.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from eidolon.memory.domain.embedding_port import (
    LOCAL_EMBEDDING_MODELS,
    EmbedderIdentity,
    EmbeddingError,
    ModelSpec,
    as_text_list,
    local_model_spec,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_MAX_TOKENS = 512
_BATCH_SIZE = 32


class OnnxSentenceEmbedder:
    """One encoder, loaded on first use.

    Loading is deferred because construction happens while the process is still
    assembling — a 100 MB session opened during import would be paid by every
    entrypoint including the ones that never embed anything. The double-checked
    lock is the same shape MemPalace uses, and for the same reason: the instance
    is shared across threads through the process-level embedder cache.
    """

    def __init__(
        self,
        model: str,
        *,
        preferred_providers: list[str] | None = None,
        intra_op_num_threads: int = 0,
        batch_size: int = _BATCH_SIZE,
        model_dir: str = "",
    ) -> None:
        key = model.strip().lower()
        spec = local_model_spec(key)
        if spec is None:
            available = ", ".join(sorted(LOCAL_EMBEDDING_MODELS))
            raise ValueError(f"unknown local embedding model {model!r}; available: {available}")
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        self._key = key
        self._spec = spec
        self._providers = preferred_providers or ["CPUExecutionProvider"]
        self._intra_op_num_threads = intra_op_num_threads
        self._batch_size = batch_size
        self._model_dir = (model_dir or "").strip()
        self._load_lock = threading.Lock()
        self._session: Any = None
        self._tokenizer: Any = None
        self._np: Any = None
        self._input_names: frozenset[str] = frozenset()

    # ── the port ─────────────────────────────────────────────────────────────

    def identity(self) -> EmbedderIdentity:
        """Cheap and available before any load.

        The collection's width is decided at creation, and a wrong value is a
        rejected write rather than a poor result — so this cannot be something
        only a loaded session can answer.
        """

        return EmbedderIdentity(
            name=self._spec.collection_name or self._key,
            dimension=self._spec.dimension,
        )

    def embed_documents(self, texts: Any) -> list[list[float]]:
        items = as_text_list(texts)
        if not items:
            # Nothing to embed, and no reason to pay the model download.
            return []
        return self._encode([self._spec.document_prefix + t for t in items])

    def embed_queries(self, texts: Any) -> list[list[float]]:
        """Queries take the query-side prefix, which for E5 is not optional."""

        items = as_text_list(texts)
        if not items:
            return []
        return self._encode([self._spec.query_prefix + t for t in items])

    # ── where the weights come from ──────────────────────────────────────────

    def _resolve_file(self, relative: str, *, subfolder: str | None, filename: str) -> str:
        """A model file's path on disk, from the configured directory or the hub.

        Resolving weights is this implementation's own business, which is the
        point of the port. It used to be done by monkeypatching
        ``huggingface_hub.hf_hub_download`` process-wide so that MemPalace's
        download call would land on a local directory — process-wide mutation to
        serve one model's file lookup. That bridge still exists for MemPalace's
        *own* embedders, which we cannot reach any other way; ours no longer need
        it.
        """

        if self._model_dir:
            root = Path(self._model_dir).expanduser()
            missing = [f for f in self._spec.files if not (root / f).is_file()]
            if missing:
                # A warning and not an error: on a machine with network the hub
                # still answers, and failing here would turn an incomplete
                # operator copy into an outage. On a machine without one, the
                # hub's own error follows immediately and this line says why it
                # was reached.
                log.warning(
                    "embedding_model_dir_incomplete",
                    model=self._key,
                    model_dir=str(root),
                    missing=missing,
                )
            else:
                return str(root / relative)

        from huggingface_hub import hf_hub_download

        if subfolder:
            return hf_hub_download(self._spec.repo, subfolder=subfolder, filename=filename)
        return hf_hub_download(self._spec.repo, filename=filename)

    def _lazy_load(self) -> None:
        if self._session is not None:
            return
        with self._load_lock:
            if self._session is not None:
                return

            try:
                import numpy as np
                import onnxruntime as ort
                from tokenizers import Tokenizer
            except ImportError as error:  # pragma: no cover - packaging failure
                raise ImportError(
                    "the local ONNX embedder needs huggingface_hub, tokenizers, "
                    "onnxruntime and numpy. All four ship with mempalace, so this "
                    "usually means one was uninstalled or pinned incompatibly."
                ) from error

            model_path = self._resolve_file(
                f"onnx/{self._spec.onnx_file}",
                subfolder="onnx",
                filename=self._spec.onnx_file,
            )
            tokenizer_path = self._resolve_file(
                "tokenizer.json", subfolder=None, filename="tokenizer.json"
            )

            options = ort.SessionOptions()
            if self._intra_op_num_threads > 0:
                options.intra_op_num_threads = self._intra_op_num_threads
            session = ort.InferenceSession(
                model_path, sess_options=options, providers=self._providers
            )

            tokenizer = Tokenizer.from_file(tokenizer_path)
            tokenizer.enable_padding()
            tokenizer.enable_truncation(max_length=_MAX_TOKENS)

            self._tokenizer = tokenizer
            self._np = np
            self._input_names = frozenset(i.name for i in session.get_inputs())
            # A decoder export declares a past-key-value input per layer per side
            # (28 × 2 for Qwen3). They are fed empty on a no-cache forward pass;
            # the shape comes from the graph's own declaration so a different
            # layer count or head dimension needs no change here.
            self._past_inputs = tuple(
                i.name for i in session.get_inputs() if "past" in i.name
            )
            self._past_shape: tuple[int, int] | None = None
            if self._past_inputs:
                declared = next(
                    i.shape for i in session.get_inputs() if i.name == self._past_inputs[0]
                )
                self._past_shape = (int(declared[1]), int(declared[3]))
            # Assigned last: the unlocked fast path above reads a non-None
            # session as "fully loaded", so everything else must be in place
            # before it becomes visible.
            self._session = session

    def _encode(self, texts: list[str]) -> list[list[float]]:
        self._lazy_load()
        np = self._np
        vectors: list[list[float]] = []

        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start : start + self._batch_size]
            encodings = self._tokenizer.encode_batch(chunk)
            ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)

            feed = {"input_ids": ids, "attention_mask": mask}
            # Every optional input below is decided by asking the session what it
            # declares, not by branching on the model family: BGE takes
            # token_type_ids and E5 does not, a decoder export takes position_ids
            # and a key-value cache and the encoders do not. Reading the graph
            # keeps a wrong feed from becoming a runtime error on a machine we did
            # not test on.
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            if "position_ids" in self._input_names:
                # Counted over real tokens so a padded row still starts at 0 on
                # its first one; a plain arange would offset every left-padded row.
                feed["position_ids"] = np.maximum(np.cumsum(mask, axis=1) - 1, 0).astype(
                    np.int64
                )
            if self._past_inputs and self._past_shape is not None:
                heads, head_dim = self._past_shape
                empty = np.zeros((ids.shape[0], heads, 0, head_dim), dtype=np.float32)
                for name in self._past_inputs:
                    feed[name] = empty

            # By name, because a decoder export returns the cache alongside the
            # hidden states and positional indexing would pick up whichever the
            # exporter happened to emit first.
            output = "last_hidden_state" if self._past_inputs else None
            hidden = self._session.run([output] if output else None, feed)[0]

            pooled = _pool(hidden, mask, self._spec, np)

            # L2-normalise so cosine distance is a dot product, which is what
            # Chroma's configured metric and the MTEB numbers both assume.
            norms = np.linalg.norm(pooled, axis=1, keepdims=True) + 1e-12
            vectors.extend((pooled / norms).tolist())

        if len(vectors) != len(texts):
            # The session returning a different number of rows than it was fed
            # would misalign every vector with its document, which no downstream
            # check catches — a fragment would be stored under another's vector.
            raise EmbeddingError(
                f"{self._key} returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors


def _pool(hidden: Any, mask: Any, spec: ModelSpec, np: Any) -> Any:
    """Reduce per-token hidden states to one vector, the way the model was trained.

    Kept as a function so the three branches read side by side. Getting this
    wrong is silent: BGE pooled by mean, or E5 pooled by CLS, still returns
    vectors of the right width that simply rank badly.
    """

    if spec.pooling == "cls":
        return hidden[:, 0]
    if spec.pooling == "last":
        # A causal model only accumulates the whole sequence at its final token —
        # a CLS position holds nothing and mean pooling dilutes it.
        #
        # Indexed off the mask rather than as ``hidden[:, -1]`` or
        # ``mask.sum() - 1``: the first is right only when the tokenizer pads
        # left, the second only when it pads right, and the wrong one reads a pad
        # position, which returns a vector instead of raising.
        last = mask.shape[1] - 1 - np.argmax(mask[:, ::-1], axis=1)
        return hidden[np.arange(hidden.shape[0]), last]
    weights = mask[..., None].astype(np.float32)
    return (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
