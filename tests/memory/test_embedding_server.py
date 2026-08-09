"""The other half of the http provider, and the wire between them.

``HttpEmbedder`` was written against a hosted endpoint that did not exist on the
board. This server is the local one, and it exists for a measurement: on the Pi 5
a memory process is 244.4 MB of which 156.4 MB is the ONNX session, paid once per
user because the supervisor spawns one process per user. Pointing those processes
at one server takes them to 88 MB each.

Most of what follows is about the seam rather than the server. Two implementations
that each look correct alone and disagree on the wire produce vectors paired with
the wrong text, and nothing downstream can tell — so the central test drives the
real client against the real app and checks the texts came back matched to
themselves.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import pytest

from eidolon.memory.domain.embedding_port import EmbedderIdentity, EmbeddingError
from eidolon.memory.entrypoints.embedding_server import (
    MAX_INPUTS_PER_REQUEST,
    EmbeddingApp,
    build_app,
)
from eidolon.memory.infrastructure.http_embedder import HttpEmbedder

DIM = 4


class _StubEncoder:
    """Stands in for the ONNX session: deterministic, and it records its input.

    What it records is the point. The server must encode exactly the bytes that
    arrived — prefixes belong to the caller — and the only way to see that is to
    look at what reached the encoder.
    """

    def __init__(self, *, fail: bool = False, hold_seconds: float = 0.0) -> None:
        self.seen: list[list[str]] = []
        self._fail = fail
        # A real encode occupies the session for milliseconds. Returning instantly
        # made the concurrency test vacuous: with nothing to overlap, removing the
        # gate entirely still measured a maximum of one. The stub has to hold for
        # the gate to have anything to hold back.
        self._hold = hold_seconds
        self._lock = threading.Lock()
        self.concurrent = 0
        self.max_concurrent = 0

    def identity(self) -> EmbedderIdentity:
        return EmbedderIdentity(name="stub_model", dimension=DIM)

    def embed_documents(self, texts: Any) -> list[list[float]]:
        if self._fail:
            raise EmbeddingError("the session did not load")
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            self.seen.append(list(texts))
        try:
            if self._hold:
                time.sleep(self._hold)
            return [_one_hot(t) for t in texts]
        finally:
            with self._lock:
                self.concurrent -= 1


def _one_hot(text: str) -> list[float]:
    """A unit vector whose set axis is decided by the text, not by its position.

    Unit length on purpose: the client L2-normalises everything it receives, so
    any encoding whose components carry magnitude comes back rescaled and can no
    longer be compared across texts. A first version of this stub did that and
    the test asserted an ordering that normalisation had already destroyed —
    the assertion was wrong, not the server. One-hot survives normalisation
    unchanged, which is what makes a transposed pairing show up as an exact
    mismatch rather than as a near-miss nobody can read.
    """

    axis = (len(text) - 1) % DIM
    return [1.0 if i == axis else 0.0 for i in range(DIM)]


def _app(**kwargs: Any) -> tuple[EmbeddingApp, _StubEncoder]:
    encoder = _StubEncoder(
        fail=kwargs.get("fail", False),
        hold_seconds=kwargs.get("hold_seconds", 0.0),
    )
    app = EmbeddingApp(
        encoder,
        model_name="stub-model",
        concurrency=kwargs.get("concurrency", 2),
    )
    return app, encoder


def _drive(app: EmbeddingApp) -> httpx.Client:
    """A sync client that runs the ASGI app in place.

    ``HttpEmbedder`` holds a sync ``httpx.Client`` and ``httpx.ASGITransport``
    only serves the async one, so the two cannot be connected directly. Bridging
    here rather than binding a real port keeps the test hermetic while still
    putting both real implementations on either end of one request.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return asyncio.run(_one_request(app, request))

    return httpx.Client(transport=httpx.MockTransport(handler))


async def _one_request(app: EmbeddingApp, request: httpx.Request) -> httpx.Response:
    scope = {
        "type": "http",
        "method": request.method,
        "path": request.url.path,
        "headers": [(k.encode(), v.encode()) for k, v in request.headers.items()],
    }
    body = request.content
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status = 500
    chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return httpx.Response(status, content=b"".join(chunks))


def _client(app: EmbeddingApp, **overrides: Any) -> HttpEmbedder:
    kwargs: dict[str, Any] = {
        "base_url": "http://127.0.0.1:8760/v1",
        "model": "stub-model",
        "dimension": DIM,
        "name": "stub_model",
        "max_retries": 0,
        "client": _drive(app),
    }
    kwargs.update(overrides)
    return HttpEmbedder(**kwargs)


# ── the seam ──────────────────────────────────────────────────────────────────


def test_the_real_client_and_the_real_server_agree_on_the_wire() -> None:
    """The test the whole file exists for.

    Each half was written against a description of the other. A disagreement
    here — a missing ``data`` key, a width, an index base — is the kind that
    stores a fragment under another fragment's vector.
    """

    app, encoder = _app()
    texts = ["第一句", "第二句话", "三"]  # lengths 3, 4, 1 -> axes 2, 3, 0
    vectors = _client(app).embed_documents(texts)

    assert encoder.seen == [texts]
    assert vectors == [_one_hot(t) for t in texts]
    assert [v.index(1.0) for v in vectors] == [2, 3, 0]


def test_a_reordered_response_still_pairs_each_text_with_its_own_vector() -> None:
    """The client places by ``index``; this proves the server numbers by position.

    If the server numbered rows some other way the client's index-placing would
    silently transpose, which is exactly the failure its own docstring warns
    about and cannot detect on its own.
    """

    app, _ = _app()
    body = json.loads(
        asyncio.run(
            _one_request(
                app,
                httpx.Request(
                    "POST",
                    "http://x/v1/embeddings",
                    json={"model": "stub-model", "input": ["a", "bb", "ccc"]},
                ),
            )
        ).content
    )

    # "a", "bb", "ccc" set axes 0, 1, 2 — so the row numbered i must carry the
    # vector of the text that was at position i in the request.
    assert [row["index"] for row in body["data"]] == [0, 1, 2]
    assert [row["embedding"].index(1.0) for row in body["data"]] == [0, 1, 2]


def test_the_server_encodes_exactly_what_arrived_prefixes_included() -> None:
    """Prefixing happens once, on the caller. The server must not add its own.

    Applied on both sides an E5 query becomes ``query: query: ...``; applied on
    neither it ranks badly and raises nothing. The rule is that the caller owns
    it, and this is what holds the server to its half.
    """

    app, encoder = _app()
    _client(app, query_prefix="query: ", document_prefix="passage: ").embed_queries(
        ["我喜欢什么茶"]
    )

    assert encoder.seen == [["query: 我喜欢什么茶"]]


# ── refusals ──────────────────────────────────────────────────────────────────


def _post(app: EmbeddingApp, payload: Any) -> httpx.Response:
    return asyncio.run(
        _one_request(
            app,
            httpx.Request("POST", "http://x/v1/embeddings", json=payload),
        )
    )


def test_an_oversized_batch_is_refused_rather_than_truncated() -> None:
    """Truncating would return fewer vectors than texts, which the client reads
    as a short body — a correct error, but one that names the wrong end."""

    app, encoder = _app()
    response = _post(app, {"input": ["x"] * (MAX_INPUTS_PER_REQUEST + 1)})

    assert response.status_code == 400
    assert str(MAX_INPUTS_PER_REQUEST) in response.json()["detail"]
    assert encoder.seen == []


def test_token_id_input_is_named_rather_than_encoded_as_text() -> None:
    app, encoder = _app()
    response = _post(app, {"input": [[1, 2, 3]]})

    assert response.status_code == 400
    assert "token-id" in response.json()["detail"]
    assert encoder.seen == []


def test_an_empty_input_is_a_request_error_not_an_empty_answer() -> None:
    app, _ = _app()
    assert _post(app, {"input": []}).status_code == 400


def test_a_failed_encode_is_a_500_so_the_client_retries_it() -> None:
    """The status decides the behaviour: ``HttpEmbedder`` retries 5xx and refuses
    to retry 4xx. A session that failed to load is worth trying again; a
    malformed request is not."""

    app, _ = _app(fail=True)
    response = _post(app, {"input": ["一句话"]})

    assert response.status_code == 500
    assert "did not load" in response.json()["detail"]


def test_an_unknown_route_is_404_not_an_encode() -> None:
    app, encoder = _app()
    response = asyncio.run(_one_request(app, httpx.Request("POST", "http://x/v1/completions")))

    assert response.status_code == 404
    assert encoder.seen == []


# ── the queue ─────────────────────────────────────────────────────────────────


def test_no_more_requests_encode_at_once_than_the_gate_allows() -> None:
    """Admitting more than the session parallelises converts a queue we can see
    into thread contention we cannot — which is the contention this server was
    built to remove, so letting it back in here would be self-defeating."""

    app, encoder = _app(concurrency=2, hold_seconds=0.05)

    async def hammer() -> None:
        await asyncio.gather(
            *(
                _one_request(
                    app,
                    httpx.Request("POST", "http://x/v1/embeddings", json={"input": [f"t{i}"]}),
                )
                for i in range(8)
            )
        )

    asyncio.run(hammer())

    assert len(encoder.seen) == 8
    assert encoder.max_concurrent <= 2


def test_health_answers_with_the_width_the_collection_was_created_at() -> None:
    app, _ = _app()
    response = asyncio.run(_one_request(app, httpx.Request("GET", "http://x/health")))

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "model": "stub_model", "dimension": DIM}


# ── the prefix footgun ────────────────────────────────────────────────────────


def test_a_prefixed_model_will_not_serve_until_the_operator_says_who_prefixes() -> None:
    """Neither side prefixing raises nothing and only ranks badly.

    This server cannot see the caller's configuration, so it cannot decide — it
    can only refuse to let the decision go unmade. The message has to carry the
    exact prefixes, because an operator who has to go and look them up is an
    operator who guesses.
    """

    with pytest.raises(ValueError) as caught:
        build_app(
            model="multilingual-e5-base",
            model_dir="",
            threads=1,
            concurrency=1,
            client_applies_prefixes=False,
        )

    message = str(caught.value)
    assert "query: " in message
    assert "passage: " in message
    assert "--client-applies-prefixes" in message


def test_an_unprefixed_model_needs_no_such_assertion(tmp_path) -> None:
    """bge-base-zh declares no prefixes, so there is nothing to get wrong and
    nothing to assert. Built, not loaded: the session is deferred to first use,
    which is what keeps this test off the weights."""

    app = build_app(
        model="bge-base-zh",
        model_dir=str(tmp_path),
        threads=1,
        concurrency=1,
        client_applies_prefixes=False,
    )

    assert app.identity.dimension == 768
    assert app.identity.name == "bge_base_zh_v15"


def test_an_existing_local_palace_can_be_moved_to_the_server_without_a_rebuild() -> None:
    """The migration this server exists to make possible, pinned at the config.

    A hosted embedder's collection name is prefixed on purpose, so a palace built
    by one implementation cannot be opened by another claiming the same model id.
    Here the weights genuinely are the same — it is our own encoder, one process
    over — so ``collection_name`` is overridden back and the existing collection
    opens. Someone removing that override as redundant would make every existing
    user's palace unreadable, and Chroma would report it as a name mismatch far
    from this decision.
    """

    from eidolon.memory.config.memory_settings import EmbeddingConfig

    local = EmbeddingConfig.model_validate({"provider": "local", "model": "bge-base-zh"})
    moved = EmbeddingConfig.model_validate(
        {
            "provider": "http",
            "model": "bge-base-zh",
            "http": {
                "base_url": "http://127.0.0.1:8760/v1",
                "model": "bge-base-zh",
                "dimension": 768,
                "collection_name": "bge_base_zh_v15",
            },
        }
    )
    without_the_override = EmbeddingConfig.model_validate(
        {
            "provider": "http",
            "model": "bge-base-zh",
            "http": {
                "base_url": "http://127.0.0.1:8760/v1",
                "model": "bge-base-zh",
                "dimension": 768,
            },
        }
    )

    assert moved.declared_identity() == local.declared_identity()
    # The guard is still doing its job for anyone who did not mean to migrate.
    assert without_the_override.declared_identity() != local.declared_identity()


def test_the_flag_is_what_lets_a_prefixed_model_through(tmp_path) -> None:
    app = build_app(
        model="multilingual-e5-base",
        model_dir=str(tmp_path),
        threads=1,
        concurrency=1,
        client_applies_prefixes=True,
    )

    assert app.identity.dimension == 768
