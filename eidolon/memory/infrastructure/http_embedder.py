"""An embedder that lives behind an OpenAI-compatible ``/v1/embeddings`` endpoint.

The second implementation of ``EmbeddingPort``, and the one that makes the port
honest. With only an in-process ONNX session, embedding could not really fail:
there was no timeout to hit, no partial response to reconcile, no width the caller
had to declare before finding out. Every one of those exists here, so the seam had
to name them rather than assume them away.

It is also the useful one on the deployment target. A Raspberry Pi 5 has four
cores shared with the rest of Eidolon, and the model that retrieves best of
everything measured (embeddinggemma, 3 GB resident) is excluded by memory alone,
not by quality. Moving the encoder off the board removes both constraints at the
cost of a network hop in the recall path — a trade this hardware may well want,
and one that could not even be evaluated while the embedder was a local class.

This is not the cloud storage that was deliberately deleted from this project.
Nothing is stored anywhere but the palace on this machine; the endpoint sees the
text of a query or a fragment and answers with a vector.

**Three failure modes get explicit handling, because each is silent otherwise:**

* A body with fewer rows than the batch, or rows out of order. The API contract
  numbers each row with an ``index`` and does not promise order, so results are
  placed by that index rather than by position. Trusting position would pair
  every fragment with another fragment's vector, and nothing downstream compares
  the two.
* A width other than the configured one. The collection was created at the
  declared width; Chroma rejects a differently-sized write, which surfaces far
  from here and reads as a storage fault. Checked on arrival instead, and named.
* A transport error or a 5xx. Retried a bounded number of times because these are
  usually transient, and then raised — an embedding that never arrives must not
  become an empty vector, which stores and recalls as "similar to nothing".

A 4xx other than 429 is not retried: a wrong model id, a rejected key or a
malformed request will be just as wrong the second time, and retrying only delays
the message that says so.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from eidolon.memory.domain.embedding_port import (
    EmbedderIdentity,
    EmbeddingError,
    as_text_list,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

#: Statuses worth a second attempt. 429 is included because a rate limit is by
#: definition temporary; everything else in the 4xx range is a request that will
#: not become valid by being repeated.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0


class HttpEmbedder:
    """A hosted encoder, reached over HTTP.

    ``identity`` is supplied rather than discovered. The name is what Chroma
    persists on the collection and the dimension is the width it is created at,
    so both have to be known before the first request — a palace cannot be built
    by asking a network service what it will probably return. The declared width
    is then checked against every response, which is the part that turns a wrong
    declaration into a clear error instead of a rejected write later.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dimension: int,
        name: str,
        api_key: str = "",
        query_prefix: str = "",
        document_prefix: str = "",
        timeout_seconds: float = 30.0,
        batch_size: int = 32,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        root = (base_url or "").strip().rstrip("/")
        if not root:
            raise ValueError("embedding.http.base_url is required for provider 'http'")
        if not (model or "").strip():
            raise ValueError("embedding.http.model is required for provider 'http'")
        if dimension < 1:
            raise ValueError(
                "embedding.http.dimension must be the width the endpoint returns; "
                "it fixes the collection's width at creation and cannot be "
                "discovered later without a network call the palace build would "
                "then depend on"
            )
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")

        self._url = f"{root}/embeddings"
        self._model = model.strip()
        self._identity = EmbedderIdentity(name=name, dimension=dimension)
        self._api_key = api_key
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._timeout = timeout_seconds
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            # The endpoint is named in configuration, so proxy settings picked up
            # from the ambient environment would silently redirect it.
            trust_env=False,
        )

    # ── the port ─────────────────────────────────────────────────────────────

    def identity(self) -> EmbedderIdentity:
        return self._identity

    def embed_documents(self, texts: Any) -> list[list[float]]:
        items = as_text_list(texts)
        if not items:
            return []
        return self._embed([self._document_prefix + t for t in items])

    def embed_queries(self, texts: Any) -> list[list[float]]:
        items = as_text_list(texts)
        if not items:
            return []
        return self._embed([self._query_prefix + t for t in items])

    def close(self) -> None:
        """Release the connection pool. Only ours to close if we opened it."""

        if self._owns_client:
            self._client.close()

    # ── the request ──────────────────────────────────────────────────────────

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start : start + self._batch_size]
            vectors.extend(self._embed_one_batch(chunk))
        return vectors

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _embed_one_batch(self, chunk: list[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": chunk}
        last_error = ""

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(
                    self._url,
                    json=payload,
                    headers=self._headers(),
                    timeout=self._timeout,
                )
            except httpx.HTTPError as error:
                # Covers timeouts, connection failures and protocol errors alike:
                # from here they are all "no vectors arrived", and the retry
                # decision is the same for all of them.
                last_error = f"{type(error).__name__}: {error}"
            else:
                if response.status_code == 200:
                    return self._vectors_from(response, chunk)
                last_error = (
                    f"HTTP {response.status_code}: {response.text[:300].strip()}"
                )
                if response.status_code not in _RETRYABLE_STATUS:
                    raise EmbeddingError(
                        f"{self._url} refused the request for model "
                        f"{self._model!r} — {last_error}. Not retried: a status "
                        f"outside {sorted(_RETRYABLE_STATUS)} means the request "
                        f"itself is wrong, so repeating it only delays this "
                        f"message."
                    )

            if attempt < self._max_retries:
                delay = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_CAP_SECONDS)
                log.warning(
                    "http_embedder_retrying",
                    url=self._url,
                    model=self._model,
                    attempt=attempt + 1,
                    of=self._max_retries,
                    delay_seconds=delay,
                    error=last_error,
                )
                time.sleep(delay)

        raise EmbeddingError(
            f"{self._url} did not return embeddings for model {self._model!r} "
            f"after {self._max_retries + 1} attempt(s) — {last_error}. The last "
            f"batch held {len(chunk)} text(s) at a {self._timeout}s timeout."
        )

    def _vectors_from(self, response: httpx.Response, chunk: list[str]) -> list[list[float]]:
        try:
            body = response.json()
        except ValueError as error:
            raise EmbeddingError(
                f"{self._url} returned a 200 that is not JSON: {error}"
            ) from error

        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise EmbeddingError(
                f"{self._url} returned a 200 without a 'data' list; got keys "
                f"{sorted(body)[:8] if isinstance(body, dict) else type(body).__name__}. "
                f"This endpoint is expected to speak the OpenAI /v1/embeddings shape."
            )
        if len(rows) != len(chunk):
            raise EmbeddingError(
                f"{self._url} returned {len(rows)} embeddings for {len(chunk)} "
                f"texts. A short body would otherwise pair each text with the "
                f"next one's vector."
            )

        # Placed by the row's own index rather than by its position in the list.
        # The API numbers them and does not promise order; a provider that
        # batches internally and reorders would otherwise silently transpose
        # vectors between documents, which nothing downstream can detect.
        placed: list[list[float] | None] = [None] * len(chunk)
        for position, row in enumerate(rows):
            if not isinstance(row, dict):
                raise EmbeddingError(f"{self._url} returned a non-object row at {position}")
            index = row.get("index", position)
            if not isinstance(index, int) or not 0 <= index < len(chunk):
                raise EmbeddingError(
                    f"{self._url} returned index {index!r} for a batch of "
                    f"{len(chunk)}; cannot say which text it belongs to."
                )
            if placed[index] is not None:
                raise EmbeddingError(
                    f"{self._url} returned index {index} twice, so at least one "
                    f"text has no vector of its own."
                )
            placed[index] = self._one_vector(row.get("embedding"), index)

        return [vector for vector in placed if vector is not None]

    def _one_vector(self, raw: Any, index: int) -> list[float]:
        if not isinstance(raw, list):
            raise EmbeddingError(
                f"{self._url} returned {type(raw).__name__} instead of a list of "
                f"floats at index {index}. Base64 embedding formats are not "
                f"requested and not accepted."
            )
        width = len(raw)
        if width != self._identity.dimension:
            raise EmbeddingError(
                f"{self._url} returned {width}-dimensional vectors but "
                f"embedding.http.dimension says {self._identity.dimension}. The "
                f"collection was created at the configured width, so this would "
                f"be rejected by the vector store as a storage fault instead of "
                f"read as a configuration one. Set the configured width to "
                f"{width} and rebuild the palace."
            )
        vector = [float(x) for x in raw]
        return _normalized(vector)


def _normalized(vector: list[float]) -> list[float]:
    """L2-normalise, so cosine distance is a dot product.

    Applied unconditionally rather than only when needed. Most hosted endpoints
    already return unit vectors, in which case this is a no-op to floating-point
    precision; the ones that do not would otherwise make similarity scores from
    this implementation incomparable with the local one's, which normalises. A
    seam whose two sides return differently scaled vectors is not a seam.
    """

    total = sum(x * x for x in vector) ** 0.5
    if total <= 0.0:
        # An all-zero vector is equally similar to everything, so it would be
        # recalled for every query. Refused rather than divided by.
        raise EmbeddingError("the endpoint returned an all-zero vector")
    return [x / total for x in vector]


def resolve_api_key(api_key_env: str) -> str:
    """Read the key out of the environment, refusing a name that resolves empty.

    Named rather than inlined so the message is the same wherever it is reached.
    An empty key would be sent as ``Bearer `` and answered with a 401, which
    reads as a credential problem at the provider rather than as a missing entry
    in ``config/.env``.
    """

    env = (api_key_env or "").strip()
    if not env:
        return ""
    value = os.environ.get(env, "").strip()
    if not value:
        raise ValueError(
            f"embedding.http.api_key_env names {env}, which is unset or empty. "
            f"Set it in config/.env, or clear api_key_env if the endpoint needs "
            f"no credential."
        )
    return value
