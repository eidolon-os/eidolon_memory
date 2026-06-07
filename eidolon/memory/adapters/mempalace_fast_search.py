"""Voice-optimized search: one query embedding, many wings (no re-embed per wing)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eidolon.memory.application.query_embedding import embed_query_vector
from eidolon.memory.domain.errors import MemoryBackendUnavailable


def search_memories_shared_embedding(
    query: str,
    palace_path: str,
    *,
    wings: list[str],
    room: str | None,
    n_results: int,
    query_embedding: list[float] | None = None,
    skip_closets: bool = True,
    collection_name: str | None = None,
) -> list[dict[str, Any]]:
    """Search multiple wings using a single precomputed query embedding."""
    try:
        from mempalace.palace import get_collection
    except ImportError as exc:
        raise MemoryBackendUnavailable("mempalace package is not installed") from exc

    embedding = query_embedding if query_embedding is not None else embed_query_vector(query)
    vec = [embedding]

    try:
        drawers_col = get_collection(palace_path, collection_name=collection_name, create=False)
        where = _combined_where(wings, room)
        limit = max(n_results * max(3, len(wings) * 3), n_results)
        try:
            drawer_results = _query_collection(
                drawers_col,
                query_embeddings=vec,
                n_results=limit,
                where=where,
            )
            post_filter = False
        except Exception:
            # Chroma 1.5.x can throw transient SQLite/HNSW errors specifically
            # on filtered queries while an unfiltered vector query still works.
            # Keep the voice path fast and useful by falling back once, then
            # applying the wing/room filter in Python.
            drawer_results = _query_collection(
                drawers_col,
                query_embeddings=vec,
                n_results=max(limit * 2, 50),
                where=None,
            )
            post_filter = True

        closet_boost_by_source = {}
        if not skip_closets:
            closet_boost_by_source = _closet_boosts(
                palace_path,
                query_embeddings=vec,
                n_results=n_results,
                where=where,
                post_filter=post_filter,
                wings=wings,
                room=room,
            )
    except Exception as exc:
        raise MemoryBackendUnavailable(str(exc)) from exc

    hits = _score_results(
        drawer_results,
        wings=wings,
        room=room,
        n_results=max(n_results * len(wings), n_results),
        closet_boost_by_source=closet_boost_by_source,
        post_filter=post_filter,
    )
    hits.sort(key=lambda h: float(h.get("similarity", 0.0)), reverse=True)
    return hits[: max(n_results * len(wings), n_results)]


def _combined_where(wings: list[str], room: str | None) -> dict[str, Any] | None:
    if not wings and not room:
        return None
    wing_filter: dict[str, Any] | None
    if not wings:
        wing_filter = None
    elif len(wings) == 1:
        wing_filter = {"wing": wings[0]}
    else:
        wing_filter = {"wing": {"$in": wings}}
    if room and wing_filter:
        return {"$and": [wing_filter, {"room": room}]}
    if room:
        return {"room": room}
    return wing_filter


def _query_collection(
    collection,
    *,
    query_embeddings: list[list[float]],
    n_results: int,
    where: dict[str, Any] | None,
):
    kwargs: dict[str, Any] = {
        "query_embeddings": query_embeddings,
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where
    return collection.query(**kwargs)


def _matches_scope(meta: dict[str, Any], *, wings: list[str], room: str | None) -> bool:
    if wings and meta.get("wing") not in set(wings):
        return False
    if room and meta.get("room") != room:
        return False
    return True


def _closet_boosts(
    palace_path: str,
    *,
    query_embeddings: list[list[float]],
    n_results: int,
    where: dict[str, Any] | None,
    post_filter: bool,
    wings: list[str],
    room: str | None,
) -> dict[str, tuple]:
    try:
        from mempalace.palace import get_closets_collection
        from mempalace.searcher import _first_or_empty

        closets_col = get_closets_collection(palace_path, create=False)
        try:
            closet_results = _query_collection(
                closets_col,
                query_embeddings=query_embeddings,
                n_results=n_results * 2,
                where=where,
            )
        except Exception:
            closet_results = _query_collection(
                closets_col,
                query_embeddings=query_embeddings,
                n_results=max(n_results * 4, 50),
                where=None,
            )
            post_filter = True

        out: dict[str, tuple] = {}
        for rank, (cdoc, cmeta, cdist) in enumerate(
            zip(
                _first_or_empty(closet_results, "documents"),
                _first_or_empty(closet_results, "metadatas"),
                _first_or_empty(closet_results, "distances"),
            )
        ):
            cmeta = cmeta or {}
            if post_filter and not _matches_scope(cmeta, wings=wings, room=room):
                continue
            source = cmeta.get("source_file", "")
            if source and source not in out:
                out[source] = (rank, cdist, (cdoc or "")[:200])
        return out
    except Exception:
        return {}


def _score_results(
    drawer_results,
    *,
    wings: list[str],
    room: str | None,
    n_results: int,
    closet_boost_by_source: dict[str, tuple],
    post_filter: bool,
) -> list[dict[str, Any]]:
    from mempalace.searcher import _first_or_empty

    closet_rank_boosts = [0.40, 0.25, 0.15, 0.08, 0.04]
    closet_distance_cap = 1.5

    scored: list[dict[str, Any]] = []
    for doc, meta, dist in zip(
        _first_or_empty(drawer_results, "documents"),
        _first_or_empty(drawer_results, "metadatas"),
        _first_or_empty(drawer_results, "distances"),
    ):
        meta = meta or {}
        if post_filter and not _matches_scope(meta, wings=wings, room=room):
            continue
        doc = doc or ""
        source = meta.get("source_file", "") or ""
        boost = 0.0
        if source in closet_boost_by_source:
            c_rank, c_dist, _preview = closet_boost_by_source[source]
            if c_dist <= closet_distance_cap and c_rank < len(closet_rank_boosts):
                boost = closet_rank_boosts[c_rank]
        effective_dist = max(0.0, min(2.0, float(dist) - boost))
        scored.append(
            {
                "text": doc,
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(source).name if source else "?",
                "distance": round(float(dist), 4),
                "similarity": round(max(0.0, 1.0 - effective_dist), 3),
                "_sort_key": effective_dist,
                "metadata": meta,
            }
        )

    scored.sort(key=lambda h: h["_sort_key"])
    trimmed = scored[:n_results]
    for h in trimmed:
        h.pop("_sort_key", None)
    return trimmed

