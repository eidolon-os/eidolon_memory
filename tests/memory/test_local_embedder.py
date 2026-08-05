"""The embedder seam, and the ways switching across it can silently not work.

Almost every failure guarded here is silent. A wrong pooling choice, an omitted
mandatory prefix, a cache key that does not match the one MemPalace computes, a
hosted endpoint returning rows out of order — none of them raise. They return
vectors that rank badly, or vectors belonging to another document, which looks
exactly like a weak model, and the palace gets built anyway.

That is not hypothetical: a palace built with ``minilm`` instead of the
configured embedder is what made every quality figure this project published
before 2026-08-03 measure the wrong thing, for four probe runs, with nothing
anywhere reporting a problem.

The file is in three parts: the port and its three implementations, the Chroma
shape that wraps any of them, and installing one into MemPalace.
"""

from __future__ import annotations

import httpx
import pytest

from eidolon.memory.config.memory_settings import (
    EmbeddingConfig,
    MemorySettings,
    hosted_collection_name,
)
from eidolon.memory.domain.embedding_port import (
    LOCAL_EMBEDDING_MODELS,
    MEMPALACE_EMBEDDING_MODELS,
    EmbeddingError,
    EmbeddingPort,
    is_local_model,
    local_model_spec,
)
from eidolon.memory.infrastructure.chroma_embedding_function import ChromaEmbeddingFunction
from eidolon.memory.infrastructure.embedder_factory import build_embedder
from eidolon.memory.infrastructure.http_embedder import HttpEmbedder
from eidolon.memory.infrastructure.mempalace_embedder import MemPalaceEmbedder
from eidolon.memory.infrastructure.onnx_sentence_embedder import OnnxSentenceEmbedder

# ── the spec table ────────────────────────────────────────────────────────────


def test_every_model_declares_a_pooling_we_implement() -> None:
    """A pooling string nothing handles falls through to mean pooling, which for a
    BGE or a decoder model is wrong in a way that only shows up as worse ranking.

    ``last`` is here because a causal model accumulates the sequence at its final
    token; ``cls`` reads a position that holds nothing for one, and ``mean``
    dilutes it.
    """

    for name, spec in LOCAL_EMBEDDING_MODELS.items():
        assert spec.pooling in {"cls", "mean", "last"}, (
            f"{name} declares pooling {spec.pooling!r}"
        )


def test_last_token_pooling_reads_the_last_real_token() -> None:
    """Not ``hidden[:, -1]`` and not ``mask.sum() - 1``.

    The first is right only when the tokenizer pads left, the second only when it
    pads right. The wrong one reads a pad position, which returns a vector rather
    than raising — so it looks like a weak model.
    """

    import numpy as np

    from eidolon.memory.infrastructure.onnx_sentence_embedder import _pool

    spec = local_model_spec("qwen3-embedding-0.6b")
    assert spec is not None and spec.pooling == "last"

    # Two rows padded on opposite sides, with a distinct value at each position so
    # the pooled row says which position was read.
    mask = np.array([[1, 1, 1, 0], [0, 1, 1, 1]])
    hidden = np.array(
        [
            [[10.0], [11.0], [12.0], [99.0]],  # right-padded: last real token is 12
            [[99.0], [20.0], [21.0], [22.0]],  # left-padded: last real token is 22
        ]
    )

    assert _pool(hidden, mask, spec, np).ravel().tolist() == [12.0, 22.0]


def test_a_decoder_model_declares_the_extra_feed_it_needs() -> None:
    """Its ONNX export takes ``position_ids`` and a key-value cache per layer.

    Asserted on the spec rather than by loading 600 MB: what this guards is that
    the model stays marked as a decoder, since the feed is decided by reading the
    session's declared inputs.
    """

    spec = local_model_spec("qwen3-embedding-0.6b")

    assert spec is not None
    assert spec.pooling == "last"
    assert spec.dimension == 1024
    assert spec.query_prefix.startswith("Instruct:")


def test_collection_names_are_distinct_across_every_implementation() -> None:
    """Chroma persists this name and refuses reads when it changes.

    Two encoders sharing one name would let a palace built with one be read with
    the other — comparing vectors from different spaces, silently. The hosted names
    are in scope because they are derived from a model id rather than written down,
    so the derivation has to be incapable of colliding with a local name.
    """

    local = [spec.collection_name for spec in LOCAL_EMBEDDING_MODELS.values()]
    theirs = [identity.name for identity in MEMPALACE_EMBEDDING_MODELS.values()]
    hosted = [
        hosted_collection_name(model)
        for model in ("bge-m3", "text-embedding-3-small", "BAAI/bge-m3")
    ]

    assert all(local) and all(theirs) and all(hosted)
    assert len(local + theirs + hosted) == len(set(local + theirs + hosted))


def test_a_hosted_name_survives_a_model_id_chroma_would_reject() -> None:
    """Model ids carry slashes, colons and dots that Chroma's name check refuses.

    A rejected name fails at collection creation, which is late but loud. The worse
    case is the one the prefix prevents: a hosted model whose sanitised id happens
    to equal a local collection name.
    """

    assert hosted_collection_name("BAAI/bge-m3:latest") == "http_baai_bge_m3_latest"
    assert hosted_collection_name("  ") == "http_embeddings"
    assert hosted_collection_name("bge_small_zh_v15") != "bge_small_zh_v15"


def test_e5_carries_the_prefixes_it_requires() -> None:
    """E5 was trained with ``query:``/``passage:`` on both sides. Dropping them
    is a measurable regression and not an error."""

    spec = local_model_spec("multilingual-e5-small")
    assert spec is not None
    assert spec.query_prefix == "query: "
    assert spec.document_prefix == "passage: "


def test_bge_carries_no_prefix() -> None:
    """Deliberate, and measured: the retrieval instruction BGE-zh accepts lowered
    every score on our corpus (0.353 → 0.312 on the most discriminating query)."""

    for name in ("bge-small-zh", "bge-base-zh"):
        spec = local_model_spec(name)
        assert spec is not None
        assert spec.query_prefix == ""
        assert spec.document_prefix == ""


def test_an_unknown_model_is_not_claimed_as_ours() -> None:
    assert is_local_model("bge-small-zh")
    assert is_local_model("  BGE-Small-ZH  ")  # settings are not case-normalised
    assert not is_local_model("minilm")
    assert not is_local_model("embeddinggemma")
    assert not is_local_model("")


# ── the port is a port ────────────────────────────────────────────────────────


def test_all_three_implementations_satisfy_the_port() -> None:
    """Checked with isinstance because the project has no type checker: a
    Protocol nothing calls ``isinstance`` on constrains nothing.

    Three implementations rather than one is the point. A seam with a single
    implementation is unproven — every assumption the one makes reads as part of
    the contract, and nothing says which.
    """

    implementations = [
        OnnxSentenceEmbedder("bge-small-zh"),
        HttpEmbedder(
            base_url="https://example.invalid/v1",
            model="hosted-bge-m3",
            dimension=1024,
            name="http_hosted_bge_m3",
        ),
        MemPalaceEmbedder(),
    ]

    for implementation in implementations:
        assert isinstance(implementation, EmbeddingPort), type(implementation).__name__


def test_the_port_does_not_carry_chromas_shape() -> None:
    """The split this refactor exists for, asserted rather than described.

    While ``name()`` and ``__call__`` lived on the encoder, "be an embedder" and
    "be a Chroma embedding function" were one obligation, and a second
    implementation had to satisfy a vector store it never talks to. Nothing
    prevents that from being glued back on except a test that notices.
    """

    for implementation in (
        OnnxSentenceEmbedder("bge-small-zh"),
        MemPalaceEmbedder(),
    ):
        assert not callable(implementation), (
            f"{type(implementation).__name__} is callable, which is Chroma's "
            f"embedding-function protocol leaking back into the port"
        )
        for chroma_only in ("name", "embed_query"):
            assert not hasattr(implementation, chroma_only), (
                f"{type(implementation).__name__}.{chroma_only} belongs to "
                f"ChromaEmbeddingFunction"
            )


def test_a_bare_string_is_refused_at_the_port() -> None:
    """``str`` satisfies ``Sequence[str]``, so this type-checks and then embeds
    character by character — one garbage vector each, no error.

    Refused here rather than wrapped, because at the port every caller is our own
    code. Chroma is the one caller that legitimately passes a bare string, and it
    is handled where Chroma is handled.
    """

    embedder = OnnxSentenceEmbedder("bge-small-zh")

    with pytest.raises(TypeError, match="character by character"):
        embedder.embed_documents("一句话")
    with pytest.raises(TypeError, match="character by character"):
        embedder.embed_queries("一句话")


# ── the local implementation ──────────────────────────────────────────────────


def test_an_unknown_model_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown local embedding model"):
        OnnxSentenceEmbedder("bge-large-martian")


def test_constructing_it_does_not_load_the_model() -> None:
    """Construction happens while the process is still assembling, and several
    entrypoints never embed anything. A 100MB session opened eagerly would be
    paid by all of them."""

    embedder = OnnxSentenceEmbedder("bge-small-zh")

    assert embedder._session is None
    # Available before any load, because the collection width is decided at
    # creation time and a wrong value is a rejected write, not a poor result.
    identity = embedder.identity()
    assert identity.dimension == 512
    assert identity.name == "bge_small_zh_v15"


def test_empty_input_does_not_trigger_a_download() -> None:
    """Chroma calls the embedding function with no documents during setup."""

    embedder = OnnxSentenceEmbedder("bge-small-zh")

    assert embedder.embed_documents([]) == []
    assert embedder.embed_queries([]) == []
    assert embedder._session is None


def test_queries_and_documents_take_their_own_prefixes() -> None:
    """The asymmetry is the whole reason the two methods exist separately."""

    embedder = OnnxSentenceEmbedder("multilingual-e5-small")
    seen: list[list[str]] = []
    embedder._encode = lambda texts: seen.append(texts) or [[0.0] * 384]  # type: ignore[method-assign]

    embedder.embed_queries(["谁"])
    embedder.embed_documents(["答案"])

    assert seen == [["query: 谁"], ["passage: 答案"]]


def test_a_configured_directory_is_read_instead_of_the_hub(tmp_path) -> None:
    """Resolving weights is the implementation's own business now.

    It used to be done by replacing ``huggingface_hub.hf_hub_download``
    process-wide — a global mutation to serve one model's file lookup, paid even on
    the default path. That bridge survives only for MemPalace's own embedders,
    which we cannot reach any other way.
    """

    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model_quantized.onnx").write_bytes(b"onnx")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    embedder = OnnxSentenceEmbedder("bge-small-zh", model_dir=str(tmp_path))

    resolved = embedder._resolve_file(
        "onnx/model_quantized.onnx", subfolder="onnx", filename="model_quantized.onnx"
    )

    assert resolved == str(tmp_path / "onnx" / "model_quantized.onnx")


def test_an_incomplete_directory_falls_back_and_says_so(tmp_path, monkeypatch) -> None:
    """A partial copy must not be half-used.

    Warned and fallen back rather than raised: on a machine with network the hub
    still answers, and failing here turns an operator's incomplete copy into an
    outage. On a machine without one, the hub's own error follows immediately and
    the warning says why it was reached.
    """

    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    embedder = OnnxSentenceEmbedder("bge-small-zh", model_dir=str(tmp_path))
    monkeypatch.setattr(
        "huggingface_hub.hf_hub_download",
        lambda repo, **kwargs: f"hub:{repo}/{kwargs.get('filename')}",
    )

    resolved = embedder._resolve_file(
        "onnx/model_quantized.onnx", subfolder="onnx", filename="model_quantized.onnx"
    )

    assert resolved == "hub:Xenova/bge-small-zh-v1.5/model_quantized.onnx"


# ── the hosted implementation ─────────────────────────────────────────────────
#
# The second implementation, and the one that made the port name its failures. A
# local ONNX session has no timeout to hit, no partial response to reconcile and
# no width the caller has to declare; every one of those exists here.


def _hosted(handler, **overrides) -> HttpEmbedder:
    kwargs = {
        "base_url": "https://embeddings.invalid/v1",
        "model": "hosted-bge-m3",
        "dimension": 3,
        "name": "http_hosted_bge_m3",
        "max_retries": 0,
        "client": httpx.Client(transport=httpx.MockTransport(handler)),
    }
    kwargs.update(overrides)
    return HttpEmbedder(**kwargs)


def _rows(vectors: list[list[float]], *, start: int = 0) -> dict:
    return {"data": [{"index": start + i, "embedding": v} for i, v in enumerate(vectors)]}


def test_the_request_is_the_openai_embeddings_shape() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json=_rows([[1.0, 0.0, 0.0]]))

    embedder = _hosted(handler, api_key="secret")
    embedder.embed_documents(["一句话"])

    assert seen[0]["url"] == "https://embeddings.invalid/v1/embeddings"
    assert seen[0]["auth"] == "Bearer secret"
    assert seen[0]["body"] == {"model": "hosted-bge-m3", "input": ["一句话"]}


def test_it_batches_rather_than_sending_one_body() -> None:
    """A batch bounds the request body, the same way the local one bounds the
    attention buffers. Which of the two a caller has is not its business."""

    batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        texts = json.loads(request.content)["input"]
        batches.append(len(texts))
        return httpx.Response(200, json=_rows([[1.0, 0.0, 0.0] for _ in texts]))

    embedder = _hosted(handler, batch_size=2)
    vectors = embedder.embed_documents(["a", "b", "c", "d", "e"])

    assert batches == [2, 2, 1]
    assert len(vectors) == 5


def test_rows_are_placed_by_their_index_not_their_position() -> None:
    """The API numbers each row and does not promise order.

    A provider that batches internally and reorders would otherwise pair every
    document with another document's vector — and nothing downstream compares the
    two, so the result is a palace of plausible, wrong vectors.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 2, "embedding": [0.0, 0.0, 3.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    {"index": 1, "embedding": [0.0, 2.0, 0.0]},
                ]
            },
        )

    vectors = _hosted(handler).embed_documents(["first", "second", "third"])

    assert [v.index(max(v)) for v in vectors] == [0, 1, 2]


def test_a_short_body_is_refused() -> None:
    """Fewer rows than texts, silently zipped, offsets every vector after it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_rows([[1.0, 0.0, 0.0]]))

    with pytest.raises(EmbeddingError, match="returned 1 embeddings for 2 texts"):
        _hosted(handler).embed_documents(["a", "b"])


def test_a_width_other_than_the_declared_one_is_refused() -> None:
    """The collection was created at the declared width.

    Chroma would reject the write, which surfaces far from here and reads as a
    storage fault. Named on arrival instead, with the width that actually came
    back, because that is the number the configuration should say.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_rows([[1.0, 0.0, 0.0, 0.0]]))

    with pytest.raises(EmbeddingError, match="returned 4-dimensional.*says 3"):
        _hosted(handler).embed_documents(["a"])


def test_a_hosted_model_takes_its_prefixes_too() -> None:
    """As mandatory for a hosted E5 as a local one, and as silent when omitted."""

    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        texts = json.loads(request.content)["input"]
        seen.append(texts)
        return httpx.Response(200, json=_rows([[1.0, 0.0, 0.0] for _ in texts]))

    embedder = _hosted(handler, query_prefix="query: ", document_prefix="passage: ")
    embedder.embed_queries(["谁"])
    embedder.embed_documents(["答案"])

    assert seen == [["query: 谁"], ["passage: 答案"]]


def test_vectors_are_normalised_so_the_two_sides_are_comparable() -> None:
    """The local implementation L2-normalises, so this one must too.

    A seam whose implementations return differently scaled vectors is not a seam:
    every similarity threshold in recall would mean something different depending
    on which one answered.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_rows([[3.0, 4.0, 0.0]]))

    vector = _hosted(handler).embed_documents(["a"])[0]

    assert vector == pytest.approx([0.6, 0.8, 0.0])


def test_an_all_zero_vector_is_refused() -> None:
    """It is equally similar to everything, so it is recalled for every query."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_rows([[0.0, 0.0, 0.0]]))

    with pytest.raises(EmbeddingError, match="all-zero"):
        _hosted(handler).embed_documents(["a"])


def test_a_timeout_is_an_error_and_not_an_empty_vector() -> None:
    """An embedding that never arrived must not become a vector.

    Returning ``[]`` would store a fragment with no vector — one that can never be
    recalled — and on the read path would answer "nothing is similar", which reads
    as an empty memory rather than a failed call.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(EmbeddingError, match="did not return embeddings"):
        _hosted(handler).embed_documents(["a"])


def test_a_5xx_is_retried_and_then_raises(monkeypatch) -> None:
    attempts: list[int] = []
    monkeypatch.setattr("eidolon.memory.infrastructure.http_embedder.time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, text="upstream busy")

    with pytest.raises(EmbeddingError, match="after 3 attempt"):
        _hosted(handler, max_retries=2).embed_documents(["a"])

    assert len(attempts) == 3


def test_a_transient_failure_that_clears_is_not_an_error(monkeypatch) -> None:
    attempts: list[int] = []
    monkeypatch.setattr("eidolon.memory.infrastructure.http_embedder.time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=_rows([[1.0, 0.0, 0.0]]))

    vectors = _hosted(handler, max_retries=2).embed_documents(["a"])

    assert len(attempts) == 2
    assert len(vectors) == 1


def test_a_4xx_is_not_retried() -> None:
    """A wrong model id or a rejected key is just as wrong the second time.

    Retrying only delays the message that says so — and on the read path it spends
    the caller's budget doing it.
    """

    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, text="no such model")

    with pytest.raises(EmbeddingError, match="refused the request"):
        _hosted(handler, max_retries=3).embed_documents(["a"])

    assert len(attempts) == 1


def test_no_request_is_made_for_no_texts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("embedding an empty batch must not reach the network")

    embedder = _hosted(handler)

    assert embedder.embed_documents([]) == []
    assert embedder.embed_queries([]) == []


def test_a_width_that_cannot_be_declared_is_refused_at_construction() -> None:
    """Not discoverable, deliberately: the width fixes the collection at creation,
    and a palace build that first has to ask a network service is a build that
    fails differently on a bad day."""

    with pytest.raises(ValueError, match="width the endpoint returns"):
        HttpEmbedder(
            base_url="https://x/v1", model="m", dimension=0, name="http_m"
        )


# ── the Chroma shape, once, around any port ───────────────────────────────────


class _StubPort:
    """A port that records which side it was asked for."""

    def __init__(self) -> None:
        self.documents: list[list[str]] = []
        self.queries: list[list[str]] = []

    def identity(self):
        from eidolon.memory.domain.embedding_port import EmbedderIdentity

        return EmbedderIdentity(name="stub_v1", dimension=2)

    def embed_documents(self, texts):
        self.documents.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_queries(self, texts):
        self.queries.append(list(texts))
        return [[0.0, 1.0] for _ in texts]


def test_the_chroma_adapter_wraps_any_port() -> None:
    """Which is what makes a second implementation cost nothing here."""

    port = _StubPort()
    ef = ChromaEmbeddingFunction(port)

    assert ef.name() == "stub_v1"
    assert ef.dimension == 2
    assert ef(["a"]) == [[1.0, 0.0]]
    assert ef.embed_documents(["b"]) == [[1.0, 0.0]]
    assert ef.embed_query(["c"]) == [[0.0, 1.0]]
    assert port.documents == [["a"], ["b"]]
    assert port.queries == [["c"]]


def test_the_adapter_takes_input_by_keyword() -> None:
    """MemPalace's ``probe_dimension`` calls ``ef(input=["probe"])``.

    Renaming the parameter is a ``TypeError`` at the moment a fresh palace is
    deciding its vector width — which is both the least recoverable moment and the
    one nothing else exercises.
    """

    ef = ChromaEmbeddingFunction(_StubPort())

    assert ef(input=["probe"]) == [[1.0, 0.0]]


def test_the_adapter_counts_a_bare_string_as_one_text() -> None:
    """Chroma really does pass one, and iterating it would return one garbage
    vector per character without error. The opposite of what the port does, and
    for the opposite reason: here the caller is not ours."""

    port = _StubPort()
    ChromaEmbeddingFunction(port)("一句话")

    assert port.documents == [["一句话"]]


def test_the_adapter_embeds_nothing_for_no_documents() -> None:
    """Chroma calls the embedding function with no documents during setup, and
    that call must not pay for a model download."""

    port = _StubPort()
    ef = ChromaEmbeddingFunction(port)

    assert ef([]) == []
    assert ef(None) == []
    assert ef.embed_query([]) == []
    assert port.documents == [] and port.queries == []


# ── the factory: one config change, a different implementation ─────────────────


def test_the_provider_decides_the_implementation() -> None:
    """The claim the whole layer exists to support, asserted directly.

    Same call, three configurations, three classes — and nothing outside this
    factory names any of them.
    """

    local = EmbeddingConfig(provider="local", model="bge-small-zh")
    hosted = EmbeddingConfig.model_validate(
        {
            "provider": "http",
            "model": "hosted-bge-m3",
            "http": {"base_url": "https://embeddings.invalid/v1", "dimension": 1024},
        }
    )
    theirs = EmbeddingConfig(provider="mempalace", model="embeddinggemma")

    assert isinstance(build_embedder(local), OnnxSentenceEmbedder)
    assert isinstance(build_embedder(hosted), HttpEmbedder)
    assert isinstance(build_embedder(theirs), MemPalaceEmbedder)


def test_an_unnamed_provider_is_read_off_the_model() -> None:
    """So a settings file that predates ``provider`` keeps its meaning."""

    assert EmbeddingConfig(model="bge-small-zh").resolved_provider() == "local"
    assert EmbeddingConfig(model="embeddinggemma").resolved_provider() == "mempalace"
    assert EmbeddingConfig(model="minilm").resolved_provider() == "mempalace"
    # Blank means "pass no model and let MemPalace apply its default", which is
    # minilm. Allowed because an existing palace may have been built that way.
    assert EmbeddingConfig(model="").resolved_provider() == "mempalace"


def test_a_hosted_endpoint_is_never_inferred() -> None:
    """A model name cannot imply a network address, so ``http`` has to be written.

    Inferring it from, say, a populated ``base_url`` would mean a leftover URL in a
    settings file silently moved the encoder off the box.
    """

    leftover = EmbeddingConfig.model_validate(
        {
            "model": "bge-small-zh",
            "http": {"base_url": "https://embeddings.invalid/v1", "dimension": 1024},
        }
    )

    assert leftover.resolved_provider() == "local"


def test_the_declared_identity_matches_what_gets_built() -> None:
    """The configuration and the implementation must agree about the name and
    width, because one of them creates the collection and the other is what the
    offline vector width and the preflight check are read from."""

    for config in (
        EmbeddingConfig(provider="local", model="multilingual-e5-large"),
        EmbeddingConfig.model_validate(
            {
                "provider": "http",
                "model": "hosted",
                "http": {"base_url": "https://x/v1", "dimension": 7, "model": "m"},
            }
        ),
    ):
        assert config.declared_identity() == build_embedder(config).identity()


def test_the_api_key_env_var_must_resolve(monkeypatch) -> None:
    """An empty key becomes ``Bearer `` and comes back a 401, which reads as a
    credential problem at the provider rather than a missing line in config/.env."""

    monkeypatch.delenv("EIDOLON_TEST_EMBEDDING_KEY", raising=False)
    config = EmbeddingConfig.model_validate(
        {
            "provider": "http",
            "model": "hosted",
            "http": {
                "base_url": "https://x/v1",
                "dimension": 4,
                "api_key_env": "EIDOLON_TEST_EMBEDDING_KEY",
            },
        }
    )

    with pytest.raises(ValueError, match="unset or empty"):
        build_embedder(config)

    monkeypatch.setenv("EIDOLON_TEST_EMBEDDING_KEY", "sk-test")
    assert isinstance(build_embedder(config), HttpEmbedder)


# ── the config section it lives in ────────────────────────────────────────────


def test_the_old_keys_still_configure_the_embedder() -> None:
    """``mempalace.embedding_*`` predates the section and is folded into it.

    Folded *before* validation, so a typo'd model name arriving under the old key
    still fails at load — which is the whole reason that validation exists.
    """

    settings = MemorySettings.model_validate(
        {
            "mempalace": {
                "embedding_model": "multilingual-e5-base",
                "embedding_device": "coreml",
                "embedding_model_dir": "/models/e5",
                "embedding_threads": 3,
            }
        }
    )

    assert settings.embedding.model == "multilingual-e5-base"
    assert settings.embedding.device == "coreml"
    assert settings.embedding.model_dir == "/models/e5"
    assert settings.embedding.threads == 3
    assert settings.embedding.resolved_provider() == "local"


def test_a_typo_under_the_old_key_still_fails_at_load() -> None:
    with pytest.raises(ValueError, match="not an encoder anything here implements"):
        MemorySettings.model_validate({"mempalace": {"embedding_model": "bge-large-martian"}})


def test_configuring_the_embedder_twice_is_refused_when_the_two_disagree() -> None:
    """Not resolved by precedence: whichever rule we picked, half the readers
    would be right and nobody could tell which half from the file."""

    with pytest.raises(ValueError, match="configured twice and the two disagree"):
        MemorySettings.model_validate(
            {
                "mempalace": {"embedding_model": "minilm"},
                "embedding": {"model": "bge-small-zh"},
            }
        )


def test_agreeing_in_both_places_is_allowed() -> None:
    """A deployment mid-migration should not have to be edited atomically."""

    settings = MemorySettings.model_validate(
        {
            "mempalace": {"embedding_model": "bge-base-zh"},
            "embedding": {"model": "bge-base-zh"},
        }
    )

    assert settings.embedding.model == "bge-base-zh"


def test_the_legacy_keys_mirror_whatever_the_section_resolved_to() -> None:
    """``entrypoints/supervisor.py`` builds a ``palace set-embedder`` argument from
    ``mempalace.embedding_model``, and the supervisor is off limits.

    Without the mirror, a deployment that writes only the new section would hand
    that command a stale default — and that command is what records which embedder
    built the palace, the one record the benchmark preflight treats as
    authoritative.
    """

    settings = MemorySettings.model_validate(
        {"embedding": {"model": "bge-base-zh", "device": "cuda", "threads": 2}}
    )

    assert settings.mempalace.embedding_model == "bge-base-zh"
    assert settings.mempalace.embedding_device == "cuda"
    assert settings.mempalace.embedding_threads == 2


def test_the_legacy_keys_are_not_part_of_the_serialised_shape() -> None:
    """Otherwise a dump-and-revalidate round trip presents the same setting twice
    and the mirror manufactures the disagreement it exists to report."""

    dumped = MemorySettings().model_dump()

    assert "embedding_model" not in dumped["mempalace"]
    assert MemorySettings.model_validate(dumped).embedding.model == "bge-small-zh"


def test_a_hosted_configuration_needs_its_address_and_width() -> None:
    with pytest.raises(ValueError, match="embedding.http.base_url"):
        MemorySettings.model_validate({"embedding": {"provider": "http", "model": "m"}})

    with pytest.raises(ValueError, match="embedding.http.dimension"):
        MemorySettings.model_validate(
            {
                "embedding": {
                    "provider": "http",
                    "model": "m",
                    "http": {"base_url": "https://x/v1"},
                }
            }
        )


def test_a_provider_that_cannot_run_the_named_model_is_refused() -> None:
    """``provider`` and ``model`` have to be changed together, and saying so at
    load is better than a palace built by whichever one won."""

    with pytest.raises(ValueError, match="'local' but embedding.model"):
        MemorySettings.model_validate(
            {"embedding": {"provider": "local", "model": "embeddinggemma"}}
        )
    with pytest.raises(ValueError, match="'mempalace' but embedding.model"):
        MemorySettings.model_validate(
            {"embedding": {"provider": "mempalace", "model": "bge-small-zh"}}
        )


def test_the_offline_vector_width_follows_the_configured_embedder() -> None:
    """A hash vector of the wrong width is a rejected write, not a poor result.

    Read off the configuration rather than one implementation's model table, so a
    hosted embedder's declared width counts too.
    """

    from eidolon.memory.adapters.mempalace_python_backend import _offline_embedding_dim

    assert _offline_embedding_dim(MemorySettings()) == 512
    assert (
        _offline_embedding_dim(
            MemorySettings.model_validate(
                {"embedding": {"provider": "mempalace", "model": "embeddinggemma"}}
            )
        )
        == 384
    )
    assert (
        _offline_embedding_dim(
            MemorySettings.model_validate(
                {
                    "embedding": {
                        "provider": "http",
                        "model": "hosted",
                        "http": {"base_url": "https://x/v1", "dimension": 1024},
                    }
                }
            )
        )
        == 1024
    )


# ── installing it into MemPalace ──────────────────────────────────────────────
#
# MemPalace picks its encoder with a hardcoded if/else and offers no registry.
# What it has is a process-level cache keyed on (model, providers), and seeding
# that is the only extension surface. These tests are about the ways that can fail
# without anyone noticing.


@pytest.fixture
def restore_upstream_cache():
    """Save and restore MemPalace's process-level embedder cache.

    Process-global, so a test that seeds it and does not clean up decides what
    every later test resolves.
    """

    import mempalace.embedding as upstream

    from eidolon.memory.infrastructure.embedder_factory import reset_active_embedder

    before = dict(upstream._EF_CACHE)
    before_dims = dict(upstream._DIM_CACHE)
    try:
        yield upstream
    finally:
        # Reassigned rather than cleared in place: one test deletes ``_EF_CACHE``
        # entirely to check that a missing upstream symbol raises, and this
        # fixture tears down before ``monkeypatch`` puts it back.
        upstream._EF_CACHE = before
        upstream._DIM_CACHE = before_dims
        reset_active_embedder()


def test_a_mempalace_model_registers_nothing(monkeypatch, restore_upstream_cache) -> None:
    """Configuring one of theirs is not an error and must not install ours.

    The model is set here rather than read from the ambient environment, because
    MemPalace is configured process-globally and anything that opened a router
    earlier in the session has already written to it. An earlier version of this
    test asserted against whatever was left over, and passed or failed by test
    order.
    """

    from eidolon.memory.infrastructure import embedder_registration

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    monkeypatch.delenv("EIDOLON_EMBEDDING_CONFIG", raising=False)

    assert embedder_registration.register_embedder() is None


def test_registration_is_resolved_by_the_public_function(
    monkeypatch, restore_upstream_cache
) -> None:
    """The point of the whole exercise.

    A key computed differently from MemPalace's own would leave the entry in a
    slot nothing reads — the palace would then be built with minilm and nothing
    would say so. So this asserts through ``get_embedding_function()``, which is
    what every MemPalace consumer actually calls, including its internals.
    """

    from eidolon.memory.infrastructure.embedder_registration import register_embedder

    upstream = restore_upstream_cache
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "bge-small-zh")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")
    monkeypatch.delenv("EIDOLON_EMBEDDING_CONFIG", raising=False)

    assert register_embedder() == "bge-small-zh"

    resolved = upstream.get_embedding_function()
    assert isinstance(resolved, ChromaEmbeddingFunction)
    assert isinstance(resolved.port, OnnxSentenceEmbedder)
    assert resolved.name() == "bge_small_zh_v15"

    # And MemPalace's identity, which is what gets written to the palace marker
    # the benchmark preflight checks.
    assert upstream.current_model_name() == "bge-small-zh"


def test_a_hosted_embedder_installs_the_same_way(monkeypatch, restore_upstream_cache) -> None:
    """The seam's actual claim: switching implementations is a config change.

    Nothing in the registration path knows which implementation it is holding.
    Upstream's cache is keyed on a model *name* and it accepts any string it does
    not recognise, so the name is a label and what sits behind it is ours.
    """

    from eidolon.memory.infrastructure.embedder_factory import active_embedder
    from eidolon.memory.infrastructure.embedder_registration import register_embedder

    upstream = restore_upstream_cache
    config = EmbeddingConfig.model_validate(
        {
            "provider": "http",
            "model": "hosted-bge-m3",
            "http": {"base_url": "https://embeddings.invalid/v1", "dimension": 1024},
        }
    )
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "hosted-bge-m3")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")

    assert register_embedder(config) == "hosted-bge-m3"

    resolved = upstream.get_embedding_function()
    assert isinstance(resolved.port, HttpEmbedder)
    assert resolved.name() == "http_hosted_bge_m3"
    assert resolved.dimension == 1024
    # And our own read path holds the same instance, not a second one — which for
    # the local implementation would be a second copy of the weights.
    assert active_embedder() is resolved.port


def test_the_two_places_the_model_name_comes_from_must_agree(
    monkeypatch, restore_upstream_cache
) -> None:
    """One is the cache key MemPalace looks up; the other decides what we put in it.

    They diverge when the environment was not applied from these settings — a bench
    that copied some sections of its child's config and not others, for instance,
    which is how the parent and the child came to disagree about the embedder once
    already.
    """

    from eidolon.memory.infrastructure.embedder_registration import (
        EmbedderRegistrationError,
        register_embedder,
    )

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "bge-base-zh")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")

    with pytest.raises(EmbedderRegistrationError, match="while embedding.model is"):
        register_embedder(EmbeddingConfig(provider="local", model="bge-small-zh"))


def test_registering_twice_reuses_the_first_instance(
    monkeypatch, restore_upstream_cache
) -> None:
    """Six call sites reach the hook; a second call must not build a second
    session."""

    from eidolon.memory.infrastructure.embedder_registration import register_embedder

    upstream = restore_upstream_cache
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "bge-small-zh")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")
    monkeypatch.delenv("EIDOLON_EMBEDDING_CONFIG", raising=False)

    register_embedder()
    first = upstream.get_embedding_function()
    register_embedder()

    assert upstream.get_embedding_function() is first


def test_a_missing_upstream_symbol_raises_instead_of_falling_back(
    monkeypatch, restore_upstream_cache
) -> None:
    """A fallback here means running on MemPalace's English-only default.

    Elsewhere we give upstream internals local fallbacks (see
    ``mempalace_compat``). Not here: degrading silently is the failure being
    prevented, so an upstream rename must stop the process.
    """

    from eidolon.memory.infrastructure.embedder_registration import (
        EmbedderRegistrationError,
        register_embedder,
    )

    upstream = restore_upstream_cache
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "bge-small-zh")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")
    monkeypatch.delenv("EIDOLON_EMBEDDING_CONFIG", raising=False)
    monkeypatch.delattr(upstream, "_EF_CACHE")

    with pytest.raises(EmbedderRegistrationError, match="no longer exposes"):
        register_embedder()


def test_a_key_that_does_not_match_is_reported(monkeypatch, restore_upstream_cache) -> None:
    """If our key ever diverges from MemPalace's, the entry is unreachable.

    Simulated by making the verification resolve something else, because the
    real divergence would come from an upstream change we cannot produce here.
    """

    from eidolon.memory.infrastructure import embedder_registration

    upstream = restore_upstream_cache
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "bge-small-zh")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")
    monkeypatch.delenv("EIDOLON_EMBEDDING_CONFIG", raising=False)
    monkeypatch.setattr(upstream, "get_embedding_function", lambda *a, **k: object())

    with pytest.raises(embedder_registration.EmbedderRegistrationError, match="resolved"):
        embedder_registration.register_embedder()


# ── carrying the choice to a child process ────────────────────────────────────


def test_the_whole_section_travels_in_one_variable() -> None:
    """The palace-init subprocess is a ``python -c``: it inherits the environment
    and none of the parent's registration, and creating the collection is exactly
    when the embedder matters.

    One variable rather than one per field, because a per-field transport is a list
    that someone eventually forgets to extend — which is the shape of the defect
    that made every quality figure before 2026-08-03 measure the wrong model.
    """

    from eidolon.memory.infrastructure.mempalace_backend import mempalace_backend_env

    settings = MemorySettings.model_validate(
        {
            "embedding": {
                "provider": "http",
                "model": "hosted-bge-m3",
                "http": {
                    "base_url": "https://embeddings.invalid/v1",
                    "dimension": 1024,
                    "timeout_seconds": 5.0,
                },
            }
        }
    )

    env = mempalace_backend_env(settings, base={})

    assert env["MEMPALACE_EMBEDDING_MODEL"] == "hosted-bge-m3"
    assert "https://embeddings.invalid/v1" in env["EIDOLON_EMBEDDING_CONFIG"]


def test_a_child_rebuilds_the_same_configuration(monkeypatch) -> None:
    from eidolon.memory.infrastructure.embedder_factory import (
        EMBEDDING_CONFIG_ENV,
        embedding_config_env_value,
        embedding_config_from_env,
    )

    original = EmbeddingConfig.model_validate(
        {
            "provider": "http",
            "model": "hosted-bge-m3",
            "http": {
                "base_url": "https://embeddings.invalid/v1",
                "dimension": 1024,
                "query_prefix": "query: ",
            },
        }
    )
    monkeypatch.setenv(EMBEDDING_CONFIG_ENV, embedding_config_env_value(original))

    assert embedding_config_from_env() == original


def test_an_environment_without_our_variable_still_describes_itself(monkeypatch) -> None:
    """The four ``MEMPALACE_EMBEDDING_*`` variables are what this project set
    before the section existed, and what a test that only names a model provides.

    Reconstructed from them rather than resolved to a default nobody chose.
    """

    from eidolon.memory.infrastructure.embedder_factory import (
        EMBEDDING_CONFIG_ENV,
        embedding_config_from_env,
    )

    monkeypatch.delenv(EMBEDDING_CONFIG_ENV, raising=False)
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "multilingual-e5-small")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_THREADS", "2")

    config = embedding_config_from_env()

    assert config.model == "multilingual-e5-small"
    assert config.threads == 2
    assert config.resolved_provider() == "local"


# ── the read path holds the port ──────────────────────────────────────────────


def test_the_query_path_embeds_on_the_query_side(monkeypatch) -> None:
    """It used to call MemPalace's embedding function, which is the *document*
    side — so an E5 query was encoded with ``passage:``.

    For BGE both prefixes are empty and nothing was wrong, which is why this went
    unnoticed: not an error, just worse ranking, indistinguishable from a weak
    model. That is the same failure class as the pooling and the wrong palace.
    """

    from eidolon.memory.adapters import mempalace_query_embedding as qe
    from eidolon.memory.infrastructure import embedder_factory

    port = _StubPort()
    monkeypatch.setattr(embedder_factory, "_active", port)
    qe.clear_embedding_cache()
    try:
        vector = qe.embed_query_vector("谁")
    finally:
        qe.clear_embedding_cache()

    assert port.queries == [["谁"]]
    assert port.documents == []
    assert vector == [0.0, 1.0]


# ── the layer edge this introduced ────────────────────────────────────────────


def test_infrastructure_does_not_import_the_adapters_package() -> None:
    """Why the embedder lives in ``infrastructure`` and not in ``adapters``.

    ``adapters/__init__.py`` eagerly imports ``mempalace_python_backend``, which
    imports ``infrastructure.mempalace_backend``. So a single module-level import
    pointing the other way closes a cycle through the package ``__init__`` —
    which is what happened when the embedder was first put in ``adapters`` and
    registered from ``mempalace_backend``. The failure was not an import error at
    startup but a ``PalaceInitError`` from a subprocess, because the cycle only
    resolved in that direction.

    Port implementations living in ``infrastructure`` is the existing convention
    anyway: all six ledgers do.

    An earlier version of this test asserted the two embedder modules did not
    import ``infrastructure``, which is the wrong invariant — it passed while the
    cycle existed.
    """

    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "eidolon/memory/infrastructure"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                if module.startswith("eidolon.memory.adapters"):
                    offenders.append(f"{path.name}:{node.lineno} → {module}")

    assert not offenders, (
        "infrastructure imports adapters, closing a cycle through "
        "adapters/__init__: " + "; ".join(offenders)
    )
