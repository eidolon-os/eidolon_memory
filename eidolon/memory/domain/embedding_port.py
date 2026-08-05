"""The port for turning text into vectors — and nothing about how.

MemPalace picks its embedder with a hardcoded if/else over two names and offers
no registry, no entry point, and no configuration hook — the same shape its
graph layer has, and the reason we own the graph. So the choice of embedder is
ours to make and ours to inject.

**What this file deliberately does not know about is Chroma.** An earlier version
of the port declared Chroma's embedding-function protocol directly — ``name()``,
``__call__``, ``embed_query``, ``embed_documents`` — which made "be an embedder"
and "be a Chroma embedding function" the same obligation. A second
implementation then had to satisfy a vector store it never talks to. The Chroma
shape now lives in exactly one place, ``infrastructure/chroma_embedding_function``,
wrapping any port.

Three properties belong to the port rather than to any implementation:

``identity().name`` is persisted by Chroma on the collection, which then refuses
reads whose embedder name differs. That is what forces a rebuild when the model
changes instead of silently comparing vectors from two different spaces — so the
name must be stable across releases and distinct across models.

``identity().dimension`` decides the collection's width at creation. Getting it
wrong is not a degraded result, it is a rejected write.

``EmbeddingError`` is declared here because embedding *can* fail. With only an
in-process ONNX session it essentially could not, so no caller had an error path
and none of them said what happens when there is no vector. A hosted endpoint
times out, rate-limits, and returns short bodies. Naming the failure on the port
is what keeps that from being each implementation's private surprise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EmbedderIdentity:
    """Which encoder this is, in the two terms a collection is built from.

    Deliberately not the settings key: ``bge-small-zh`` is what an operator
    writes, ``bge_small_zh_v15`` is what Chroma stores and compares. Keeping them
    separate is what lets the settings key be renamed without invalidating every
    palace on disk.
    """

    name: str
    dimension: int


class EmbeddingError(RuntimeError):
    """An embedding call did not produce vectors.

    Distinct from a configuration error, which is raised at construction: this
    one means the encoder exists and the call failed anyway — a timeout, a
    refused request, a body with fewer rows than texts. Callers on the read path
    surface it as an unavailable backend; callers on the write path let the turn
    go to the dead-letter queue, because a fragment stored without its vector is
    a fragment that can never be recalled.
    """


@runtime_checkable
class EmbeddingPort(Protocol):
    """A text encoder.

    ``embed_documents`` and ``embed_queries`` are separate because
    retrieval-trained models treat the two sides differently — E5 requires
    ``query:``/``passage:`` prefixes, BGE-zh accepts an optional query
    instruction. An implementation whose model makes no distinction is free to
    route both through one call, and two of the three here do.

    Both take a sequence and return one vector per text, in order. Batching is
    the implementation's business: the ONNX one batches to bound the attention
    buffers, the HTTP one batches to bound the request body, and a caller should
    not have to know which.
    """

    def identity(self) -> EmbedderIdentity:
        """The name a collection is stamped with, and its vector width.

        A method rather than an attribute because an implementation may have to
        ask something — MemPalace's own embedders only report their width by
        embedding a probe string.
        """

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


def as_text_list(texts: Sequence[str]) -> list[str]:
    """Normalise a port argument, refusing a bare string.

    A ``str`` satisfies ``Sequence[str]``, so passing one where a list belongs
    type-checks and then iterates character by character — one garbage vector per
    character, no error, and a caller who sees plausible-looking output. Raised
    rather than silently wrapped, because at the port every caller is our own
    code and passing one text as a list costs nothing. The one place a bare
    string is legitimate is Chroma's embedding-function protocol, which really
    does pass them, and that is handled where Chroma is handled.
    """

    if isinstance(texts, str):
        raise TypeError(
            "embed_documents/embed_queries take a sequence of texts; a bare "
            "string would be embedded character by character. Pass [text]."
        )
    return list(texts)


@dataclass(frozen=True)
class ModelSpec:
    """A model's identity and the conventions it was trained under.

    Description, not implementation. It lives in ``domain`` because the config
    validator and the ONNX implementation both need it and neither should depend
    on the other — config must not reach into ``infrastructure``, and a table of
    repository names is data either way.

    ``pooling`` and the two prefixes are the fields whose being wrong produces no
    error at all, only vectors that rank badly — indistinguishable from a weak
    model. A first probe of bge-small measured 0.009 of separation between a
    relevant and an irrelevant fragment because of it.
    """

    repo: str
    dimension: int
    pooling: str  # "cls" for BGE, "mean" for E5, "last" for a decoder
    query_prefix: str = ""
    document_prefix: str = ""
    collection_name: str = ""  # persisted by Chroma; defaults to the settings key
    onnx_file: str = "model_quantized.onnx"
    # Everything a fully populated local directory must hold, relative to its
    # root, so an incomplete copy is reported rather than half-used.
    files: tuple[str, ...] = ("onnx/model_quantized.onnx", "tokenizer.json")


# Keyed by the value of ``embedding.model`` in settings, for ``provider: local``.
#
# Only models measured on the real corpus are listed. The comparison, the
# reasoning, and what none of them fix are in
# ``infrastructure/onnx_sentence_embedder.py``.
LOCAL_EMBEDDING_MODELS: dict[str, ModelSpec] = {
    # The default, chosen on cost: ~130 MB resident and ~1 ms per call. Its
    # retrieval is indistinguishable from the larger two at our sample size — see
    # infrastructure/onnx_sentence_embedder.py for why that is a measurement
    # limit rather than a tie.
    #
    # BGE-zh was trained with an optional retrieval instruction on the query
    # side. Measured here it *lowered* every score (0.353 → 0.312 on the query
    # that discriminates best), so it is deliberately not applied.
    "bge-small-zh": ModelSpec(
        repo="Xenova/bge-small-zh-v1.5",
        dimension=512,
        pooling="cls",
        collection_name="bge_small_zh_v15",
    ),
    # ~130 MB and ~3 ms. No measurable retrieval advantage over small on 43
    # queries; kept switchable for a host that wants to re-measure on more.
    "bge-base-zh": ModelSpec(
        repo="Xenova/bge-base-zh-v1.5",
        dimension=768,
        pooling="cls",
        collection_name="bge_base_zh_v15",
    ),
    # 326M parameters, 1024-dim. Probed rather than assumed, and the answer was
    # that scaling buys nothing measurable here: ~3.7x the memory and ~8x the
    # latency of small, within the same ±3 noise band on both corpora.
    "bge-large-zh": ModelSpec(
        repo="Xenova/bge-large-zh-v1.5",
        dimension=1024,
        pooling="cls",
        collection_name="bge_large_zh_v15",
    ),
    # A decoder embedder — last-token pooling, instruction-aware queries, and an
    # ONNX export that declares position_ids and a 56-tensor key-value cache. Not
    # a bigger BGE. ~1.1 GB resident and an order of magnitude slower per call, so
    # it is only a candidate where that memory is available.
    "qwen3-embedding-0.6b": ModelSpec(
        repo="onnx-community/Qwen3-Embedding-0.6B-ONNX",
        dimension=1024,
        pooling="last",
        # The documented format. Measured on two corpora it gained 5 top-5 on one
        # and lost 2 on the other, so it is applied because the model was trained
        # that way, not because the gain is established.
        query_prefix=(
            "Instruct: Given a question about the user's life, retrieve the "
            "memories that answer it\nQuery: "
        ),
        collection_name="qwen3_embedding_0_6b",
    ),
    # BAAI's successor line: newer than the zh-v1.5 models by four months and what
    # they point people at now. Multilingual rather than Chinese-specific, 8192
    # context, CLS pooling, no instruction needed on either side.
    "bge-m3": ModelSpec(
        repo="onnx-community/bge-m3-ONNX",
        dimension=1024,
        pooling="cls",
        collection_name="bge_m3",
    ),
    # Alibaba's multilingual encoder, and the newest architecture in this table.
    "gte-multilingual-base": ModelSpec(
        repo="onnx-community/gte-multilingual-base",
        dimension=768,
        pooling="cls",
        collection_name="gte_multilingual_base",
    ),
    # E5 is the one family whose prefixes are mandatory rather than optional: it
    # was trained with them on both sides, and omitting them is a silent
    # regression of the kind described above.
    "multilingual-e5-small": ModelSpec(
        repo="Xenova/multilingual-e5-small",
        dimension=384,
        pooling="mean",
        query_prefix="query: ",
        document_prefix="passage: ",
        collection_name="multilingual_e5_small",
    ),
    "multilingual-e5-base": ModelSpec(
        repo="Xenova/multilingual-e5-base",
        dimension=768,
        pooling="mean",
        query_prefix="query: ",
        document_prefix="passage: ",
        collection_name="multilingual_e5_base",
    ),
    "multilingual-e5-large": ModelSpec(
        repo="Xenova/multilingual-e5-large",
        dimension=1024,
        pooling="mean",
        query_prefix="query: ",
        document_prefix="passage: ",
        collection_name="multilingual_e5_large",
    ),
}


#: MemPalace's own two, from the if/else in its ``get_embedding_function``.
#:
#: Their widths and Chroma names are duplicated from upstream rather than probed,
#: because both are needed before anything is loaded: the offline test embedder
#: has to emit vectors of the width the collection was created with, and a width
#: that disagrees is a rejected write rather than a poor result. ``minilm``
#: reports itself to Chroma as ``default`` — upstream spoofs the name so one
#: class can serve palaces built by Chroma's own DefaultEmbeddingFunction.
MEMPALACE_EMBEDDING_MODELS: dict[str, EmbedderIdentity] = {
    "minilm": EmbedderIdentity(name="default", dimension=384),
    "embeddinggemma": EmbedderIdentity(name="embeddinggemma_300m", dimension=384),
}


def local_model_spec(model: str) -> ModelSpec | None:
    """The spec for ``model``, or ``None`` if we do not implement that name."""

    return LOCAL_EMBEDDING_MODELS.get(model.strip().lower())


def is_local_model(model: str) -> bool:
    """Whether ``model`` is one we implement rather than one MemPalace does."""

    return local_model_spec(model) is not None


def mempalace_model_identity(model: str) -> EmbedderIdentity | None:
    """The identity of one of MemPalace's own encoders, or ``None``."""

    return MEMPALACE_EMBEDDING_MODELS.get(model.strip().lower())
