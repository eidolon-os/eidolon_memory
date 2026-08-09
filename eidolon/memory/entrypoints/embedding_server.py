"""One encoder for the whole board, behind ``/v1/embeddings``.

``HttpEmbedder`` has existed since the embedder was isolated behind a port, and
until now there was nothing on this side of it — the http provider could only
point at a hosted service. This is the local other half, and it exists because of
one measurement on the deployment target.

On the Raspberry Pi 5 a memory process costs **244.4 MB** resident, of which
**156.4 MB is the bge-base-zh session**. The supervisor spawns one
``agent_runner`` per user (``--memory-space-id`` is singular), so every user pays
for their own copy of identical weights. Configured with ``provider: http``
against this server, the same process costs **91 MB** — measured, not projected.
The server itself is 216 MB, once for the board, so the crossover is at two
users: ten go from 2.44 GB to 1.12 GB on a board with 8 GB shared across all of
Eidolon.

The second reason turned out to be larger. Each user's process runs its own ONNX
session with ``threads: 4`` on a four-core board, so N users mean N×4 threads
over 4 cores. From ``benchmarks/suites/probe_shared_embedder.py`` on the Pi 5,
p50 wall for one round of concurrent queries:

===========  ==================  ==================  =======
callers      private per user    one shared server   ratio
===========  ==================  ==================  =======
1             14.1 ms             16.7 ms            1.18x
2             58.5 ms             35.7 ms            0.61x
4            116.0 ms             70.4 ms            0.61x
8            218.5 ms            136.6 ms            0.63x
16           474.2 ms            260.7 ms            0.55x
===========  ==================  ==================  =======

The absolute figures move by 10% or so between runs; the ratio does not. So the
loopback hop costs about 2.6 ms and is the only cost — at one caller, and nowhere
else. From two onward the shared server is roughly 1.6x faster and the gap widens
with load. A board serving one user should not run this; a board serving two
should.

**Prefixes are the client's, not this server's.** ``embed_documents`` here is a
raw encode of exactly the bytes that arrived; ``embedding.http.query_prefix`` and
``document_prefix`` on the caller are the only place a model's convention is
applied. That rule is chosen because it is the only one that behaves identically
against this server and against a third-party endpoint, which is the whole reason
the http provider exists — the OpenAI wire format has no query/document
distinction to carry the decision on. For a model whose ``ModelSpec`` declares
prefixes it is also a live footgun: prefix on neither side ranks badly and raises
nothing, so ``--client-applies-prefixes`` makes the operator say out loud that
they configured the other end. See ``_refuse_silent_prefix_loss``.

This is not the cloud storage that was deliberately removed from this project.
Nothing is stored here and nothing leaves the board: the server binds loopback by
default, holds no palace, opens no ledger, and answers a vector for a string.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.domain.embedding_port import EmbeddingError, local_model_spec
from eidolon.memory.infrastructure.onnx_sentence_embedder import OnnxSentenceEmbedder
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]

#: Refused rather than truncated. A body this large is a caller that did not
#: batch, and encoding it would hold the single session for seconds while every
#: other user's recall waits behind it.
MAX_INPUTS_PER_REQUEST = 256
MAX_BODY_BYTES = 4 * 1024 * 1024


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _send_json(send: AsgiSend, status: int, payload: dict[str, Any]) -> None:
    body = _json_bytes(payload)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive: AsgiReceive) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ConnectionError("client disconnected before the body arrived")
        chunks.append(message.get("body", b""))
        size += len(chunks[-1])
        if size > MAX_BODY_BYTES:
            raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
        if not message.get("more_body", False):
            return b"".join(chunks)


class _BadRequest(Exception):
    """A 400 with a message the operator can act on."""


def _inputs_from(body: Any) -> list[str]:
    """The texts to encode, in the order the response must number them.

    OpenAI accepts a bare string or a list of strings; token-id input is a third
    form this server does not implement, and saying so is better than encoding
    ``[1, 2, 3]`` as the literal text of a list.
    """

    if not isinstance(body, dict):
        raise _BadRequest("body must be a JSON object")
    raw = body.get("input")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise _BadRequest("'input' must be a non-empty string or list of strings")
    if len(raw) > MAX_INPUTS_PER_REQUEST:
        raise _BadRequest(
            f"'input' holds {len(raw)} texts; this server encodes at most "
            f"{MAX_INPUTS_PER_REQUEST} per request so one caller cannot hold the "
            f"session while every other recall waits"
        )
    for position, item in enumerate(raw):
        if not isinstance(item, str):
            raise _BadRequest(
                f"'input'[{position}] is {type(item).__name__}; only text is "
                f"accepted (token-id input is not implemented)"
            )
    return raw


#: How many encodes may be in flight. Measured on the Pi 5, eight concurrent
#: callers, p50 wall for the round: gate 1 → 135.7 ms, 2 → 135.8, 4 → 126.4,
#: 8 → 119.7. At sixteen callers: 4 → 248.6, 8 → 233.3, 16 → 233.2, 32 → 226.5.
#:
#: This default was first set to 2 on the reasoning that the session already
#: parallelises one encode across its own threads, so admitting more would turn a
#: queue we can see into thread contention we cannot. The measurement says
#: otherwise and the reasoning was wrong: ONNX Runtime releases the GIL for the
#: duration of a Run, so overlapping requests spend their JSON parsing and HTTP
#: framing *beside* someone else's compute rather than after it. A gate of 2 left
#: cores idle between requests.
#:
#: Eight is where the curve flattens at both loads. Higher still buys about 3%
#: and costs an unbounded number of in-flight bodies held in memory, which is the
#: thing a board cannot spare.
DEFAULT_CONCURRENCY = 8


class EmbeddingApp:
    """The ASGI surface. One encoder, one bounded queue in front of it.

    The queue is what keeps a burst from becoming an unbounded number of live
    request bodies, and what makes a slow caller show up as its own latency
    rather than as everyone else's. See ``DEFAULT_CONCURRENCY`` for where the
    bound came from — it was reasoned first and measured second, and the
    measurement disagreed.
    """

    def __init__(
        self, embedder: Any, *, model_name: str, concurrency: int = DEFAULT_CONCURRENCY
    ) -> None:
        self._embedder = embedder
        self._model_name = model_name
        self._gate = asyncio.Semaphore(concurrency)
        self._identity = embedder.identity()

    @property
    def identity(self) -> Any:
        """The name and width the collection was created at."""

        return self._identity

    def warm_up(self) -> None:
        """Load the session now, so the first caller does not pay for it."""

        self._embedder.embed_documents(["预热"])

    async def __call__(self, scope: dict[str, Any], receive: AsgiReceive, send: AsgiSend) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        if scope["type"] != "http":
            await _send_json(send, 404, {"detail": "not found"})
            return

        path, method = scope.get("path", ""), scope.get("method", "")

        # Cheap enough to answer while the session is busy encoding: a health
        # check that queues behind real work reports the queue, not the process.
        if method == "GET" and path in ("/health", "/v1/health"):
            await _send_json(
                send,
                200,
                {
                    "status": "ok",
                    "model": self._identity.name,
                    "dimension": self._identity.dimension,
                },
            )
            return

        if method != "POST" or path not in ("/v1/embeddings", "/embeddings"):
            await _send_json(send, 404, {"detail": "not found"})
            return

        try:
            raw = await _read_body(receive)
        except (ValueError, ConnectionError) as error:
            await _send_json(send, 413, {"detail": str(error)})
            return

        try:
            texts = _inputs_from(json.loads(raw))
        except json.JSONDecodeError as error:
            await _send_json(send, 400, {"detail": f"body is not JSON: {error}"})
            return
        except _BadRequest as error:
            await _send_json(send, 400, {"detail": str(error)})
            return

        try:
            async with self._gate:
                # to_thread and not inline: encoding is blocking CPU work, and
                # running it on the event loop would stall the health check and
                # every other connection for the duration.
                vectors = await asyncio.to_thread(self._embedder.embed_documents, texts)
        except EmbeddingError as error:
            # 500 rather than 400 — the request was well formed and this end
            # failed. The client retries a 500 and does not retry a 400, and
            # retrying is the right behaviour for a session that failed to load.
            log.exception("embedding_server_encode_failed", texts=len(texts), error=str(error))
            await _send_json(send, 500, {"detail": str(error)})
            return

        await _send_json(
            send,
            200,
            {
                "object": "list",
                "model": self._model_name,
                "data": [
                    {"object": "embedding", "index": i, "embedding": v}
                    for i, v in enumerate(vectors)
                ],
                # Present because the wire shape declares it. This server does
                # not meter anything, and reporting a token count it did not
                # measure would be worse than reporting zero.
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )


def _refuse_silent_prefix_loss(model: str, *, client_applies_prefixes: bool) -> None:
    """Stop a prefixed model from being served as if it had no convention.

    A wrong prefix raises nothing. It produces vectors that rank badly, which is
    indistinguishable from a weak model — one probe of bge-small measured 0.009
    of separation between a relevant and an irrelevant fragment for exactly this
    reason. Since this server cannot see the caller's configuration, it cannot
    decide; it can only refuse to let the decision go unmade.

    Not an unconditional refusal, because a caller with the prefixes configured
    is a correct deployment and blocking it would be wrong. The flag is the
    operator asserting they did that.
    """

    spec = local_model_spec(model.strip().lower())
    if spec is None or not (spec.query_prefix or spec.document_prefix):
        return
    if client_applies_prefixes:
        return
    raise ValueError(
        f"{model} is trained with prefixes (query {spec.query_prefix!r}, document "
        f"{spec.document_prefix!r}) and this server encodes exactly what it "
        f"receives. Applied on neither side they raise nothing and only rank "
        f"badly. Set them on every caller:\n\n"
        f"  embedding:\n"
        f"    provider: http\n"
        f"    http:\n"
        f"      query_prefix: {spec.query_prefix!r}\n"
        f"      document_prefix: {spec.document_prefix!r}\n\n"
        f"then start this server with --client-applies-prefixes to say so."
    )


def build_app(
    *,
    model: str,
    model_dir: str,
    threads: int,
    concurrency: int,
    client_applies_prefixes: bool,
) -> EmbeddingApp:
    """The app, with its encoder built but not yet loaded.

    Construction is deliberately cheap — ``OnnxSentenceEmbedder`` defers the
    session to first use — so binding the port does not wait on the weights. The
    warm-up in ``main`` is what turns that back into a server that is slow only
    before it is listening.
    """

    _refuse_silent_prefix_loss(model, client_applies_prefixes=client_applies_prefixes)
    embedder = OnnxSentenceEmbedder(
        model,
        intra_op_num_threads=threads,
        model_dir=model_dir,
    )
    return EmbeddingApp(embedder, model_name=model, concurrency=concurrency)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_memory_settings()
    parser = argparse.ArgumentParser(
        prog="eidolon-memory-embedder",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Loopback by default. This server holds the text of every fragment and
    # every query on the board on its way to a vector; a default of 0.0.0.0
    # would put that on the network because someone did not pass --host.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--model", default=settings.embedding.model)
    parser.add_argument("--model-dir", default=settings.embedding.model_dir)
    parser.add_argument("--threads", type=int, default=settings.embedding.threads)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"requests encoding at once; the rest queue (default {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--client-applies-prefixes",
        action="store_true",
        help="assert that callers set embedding.http.query_prefix/document_prefix",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = _parse_args(argv)
    try:
        app = build_app(
            model=args.model,
            model_dir=args.model_dir,
            threads=args.threads,
            concurrency=args.concurrency,
            client_applies_prefixes=args.client_applies_prefixes,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error

    # Load before listening. Otherwise the first user's recall pays the whole
    # session load — seconds on this board — and the config that would have
    # failed at startup fails inside somebody's conversation instead.
    app.warm_up()
    identity = app.identity
    log.info(
        "embedding_server_start",
        host=args.host,
        port=args.port,
        model=args.model,
        collection_name=identity.name,
        dimension=identity.dimension,
        threads=args.threads,
        concurrency=args.concurrency,
    )
    print(
        f"eidolon-memory-embedder  {args.model} -> {identity.name} "
        f"({identity.dimension}d)  http://{args.host}:{args.port}/v1/embeddings"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
