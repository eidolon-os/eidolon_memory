"""Present any ``EmbeddingPort`` as a Chroma embedding function.

This is the whole of what Chroma requires, in one place. It used to be mixed into
the ONNX encoder, which meant a second implementation inherited an obligation to
a vector store it never talks to — and that the two contracts could only be told
apart by reading which method Chroma happened to call.

Four members, and each one is called by somebody specific:

* ``name()`` — Chroma persists it on the collection and refuses reads from a
  differently-named function. That refusal is the mechanism that forces a rebuild
  when the model changes, so it must be cheap and it must never be guessed. It is
  resolved once at construction, before anything is loaded, because Chroma asks
  for it while creating the collection.
* ``__call__(input=...)`` — the embedding call itself, documents side. The
  parameter really is named ``input``: MemPalace's ``probe_dimension`` calls
  ``ef(input=["probe"])`` by keyword, so renaming it is a ``TypeError`` at the
  moment a fresh palace is deciding its vector width.
* ``embed_documents`` / ``embed_query`` — what MemPalace's own ingest and search
  paths call. The asymmetry is the reason they are separate: the query side takes
  the query-side prefix, which for E5 is not optional.

A bare string is wrapped rather than refused here, which is the opposite of what
the port does. Chroma genuinely passes one, and the alternative is not an error
but an iteration over characters returning one garbage vector each.
"""

from __future__ import annotations

from typing import Any

from eidolon.memory.domain.embedding_port import EmbeddingPort


class ChromaEmbeddingFunction:
    """One port, wearing the shape Chroma and MemPalace call."""

    def __init__(self, port: EmbeddingPort) -> None:
        self._port = port
        # Resolved eagerly: Chroma asks for the name while creating a collection,
        # and an identity that had to be computed then would either block on a
        # model load or, worse, answer differently before and after one.
        self._identity = port.identity()

    @property
    def port(self) -> EmbeddingPort:
        """The implementation underneath, for code that has a port's contract."""

        return self._port

    def name(self) -> str:
        return self._identity.name

    @property
    def dimension(self) -> int:
        return self._identity.dimension

    def __call__(self, input: Any = None) -> list[list[float]]:  # noqa: A002 - Chroma's protocol
        texts = _as_texts(input)
        if not texts:
            # Chroma calls the embedding function with no documents during setup.
            # Returning early also keeps that call from paying for a model
            # download that nothing is waiting on.
            return []
        return self._port.embed_documents(texts)

    def embed_documents(self, input: Any) -> list[list[float]]:  # noqa: A002 - Chroma's protocol
        return self(input)

    def embed_query(self, input: Any) -> list[list[float]]:  # noqa: A002 - Chroma's protocol
        texts = _as_texts(input)
        if not texts:
            return []
        return self._port.embed_queries(texts)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"ChromaEmbeddingFunction(name={self._identity.name!r}, "
            f"dimension={self._identity.dimension}, "
            f"port={type(self._port).__name__})"
        )


def _as_texts(value: Any) -> list[str]:
    """Chroma's loose argument, narrowed to the list the port takes."""

    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    # ``len`` rather than truthiness: a numpy-backed documents sequence raises on
    # ambiguous truth value, which upstream hit and worked around the same way.
    return list(value) if len(value) else []
