"""Embed a query once per recall round (shared across wings)."""

from __future__ import annotations

from functools import lru_cache

from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=128)
def _embed_query_cached_normalized(query: str) -> tuple[float, ...]:
    from mempalace.embedding import get_embedding_function

    ef = get_embedding_function()
    vectors = ef([query])
    empty_msg = "embedding returned empty vector"
    if not vectors:
        raise RuntimeError(empty_msg)
    first = vectors[0]
    if first is None:
        raise RuntimeError(empty_msg)
    try:
        dim = len(first)
    except TypeError:
        dim = 0
    if dim == 0:
        raise RuntimeError(empty_msg)
    return tuple(float(x) for x in first)


def embed_query_vector(query: str) -> list[float]:
    """Return embedding for ``query``; process-local LRU cache by exact text."""
    normalized = query.strip()
    if not normalized:
        msg = "query cannot be blank"
        raise ValueError(msg)
    return list(_embed_query_cached_normalized(normalized))


def clear_embedding_cache() -> None:
    """Tests: drop cached query vectors."""
    _embed_query_cached_normalized.cache_clear()
