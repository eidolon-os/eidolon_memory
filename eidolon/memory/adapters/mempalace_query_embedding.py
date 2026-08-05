"""The query embedding the scoped multi-wing search shares across wings.

One embedding per query rather than one per wing, which is the whole point: the
voice path searches several wings under a 300 ms deadline and re-encoding the same
text per wing is the easiest way to spend that budget on nothing.

It goes through our own port now. It used to call
``mempalace.embedding.get_embedding_function()`` — routing our read path through a
library whose choice of embedder we had already had to override, and one call
further from the encoder than necessary. Seeding MemPalace's cache is still right
for its *internal* ingest and search, which we do not control; our own calls have
no reason to be among them.

**That move also fixed a real defect.** ``get_embedding_function()`` returns
Chroma's embedding function, and calling it — ``ef([query])`` — is its *document*
side. So a query was being encoded with the document-side prefix. For BGE both
prefixes are empty and nothing was wrong; for E5, whose ``query:``/``passage:``
prefixes are mandatory rather than optional, every query was encoded as a passage.
That is the exact failure class this project keeps meeting: not an error, just
worse ranking, indistinguishable from a weak model.
"""

from __future__ import annotations

from functools import lru_cache

from eidolon.memory.domain.embedding_port import EmbeddingError
from eidolon.memory.infrastructure.embedder_factory import active_embedder


@lru_cache(maxsize=128)
def _embed_query_cached_normalized(query: str) -> tuple[float, ...]:
    vectors = active_embedder().embed_queries([query])
    empty_msg = "embedding returned empty vector"
    if not vectors:
        raise EmbeddingError(empty_msg)
    first = vectors[0]
    if first is None:
        raise EmbeddingError(empty_msg)
    try:
        dim = len(first)
    except TypeError:
        dim = 0
    if dim == 0:
        raise EmbeddingError(empty_msg)
    # ``float(x)`` per element rather than ``list(first)``: an implementation may
    # return a numpy row, and the tuple this caches has to be hashable and
    # comparable regardless of which one answered.
    return tuple(float(x) for x in first)


def embed_query_vector(query: str) -> list[float]:
    """Return an embedding for ``query``; cache by normalized exact text."""
    normalized = query.strip()
    if not normalized:
        msg = "query cannot be blank"
        raise ValueError(msg)
    return list(_embed_query_cached_normalized(normalized))


def clear_embedding_cache() -> None:
    """Drop process-local cached query vectors (primarily for tests)."""
    _embed_query_cached_normalized.cache_clear()
