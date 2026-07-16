"""Voice-optimized search: one query embedding, many wings (no re-embed per wing)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eidolon.memory.adapters.mempalace_query_embedding import embed_query_vector
from eidolon.memory.domain.errors import MemoryBackendUnavailable
from eidolon.memory.infrastructure.mempalace_compat import (
    collection_metric,
    distance_similarity,
    first_result_list,
)
from eidolon.memory.infrastructure.mempalace_hnsw import probe_hnsw_safety


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
    hnsw_safety = probe_hnsw_safety(palace_path, collection_name)
    if hnsw_safety.vector_disabled:
        return _search_sqlite_fallback(
            query,
            palace_path,
            wings=wings,
            room=room,
            n_results=n_results,
            collection_name=collection_name,
        )

    try:
        from mempalace.palace import get_collection
    except ImportError as exc:
        raise MemoryBackendUnavailable("mempalace package is not installed") from exc

    if query_embedding is None:
        embedding = embed_query_vector(query)
    else:
        embedding = query_embedding
    vec = [embedding]

    try:
        drawers_col = get_collection(palace_path, collection_name=collection_name, create=False)
        metric = collection_metric(drawers_col)
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
        metric=metric,
    )
    hits.sort(key=lambda h: float(h.get("similarity", 0.0)), reverse=True)
    return hits[: max(n_results * len(wings), n_results)]


def _search_sqlite_fallback(
    query: str,
    palace_path: str,
    *,
    wings: list[str],
    room: str | None,
    n_results: int,
    collection_name: str | None,
) -> list[dict[str, Any]]:
    """Use MemPalace's SQLite BM25 path without opening an unsafe HNSW segment."""

    try:
        from mempalace.searcher import search_memories
    except ImportError as exc:
        raise MemoryBackendUnavailable("mempalace package is not installed") from exc

    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for wing in wings or [None]:
        payload = search_memories(
            query=query,
            palace_path=palace_path,
            wing=wing,
            room=room,
            n_results=n_results,
            vector_disabled=True,
            collection_name=collection_name,
        )
        if isinstance(payload, dict) and payload.get("error"):
            raise MemoryBackendUnavailable(str(payload["error"]))
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            key = (
                str(row.get("text") or ""),
                str(row.get("wing") or wing or ""),
                str(row.get("room") or ""),
                str(row.get("source_path") or row.get("source_file") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            hits.append(row)

    def _rank(row: dict[str, Any]) -> float:
        for key in ("hybrid_score", "bm25_score", "similarity"):
            value = row.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return 0.0

    hits.sort(key=_rank, reverse=True)
    return hits[: max(n_results * max(1, len(wings)), n_results)]


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


def _distance_to_similarity(distance: float | None, metric: str = "cosine") -> float:
    return distance_similarity(distance, metric)


def _apply_distance_boost(distance: float, boost: float, metric: str) -> float:
    effective = float(distance) - boost
    if (metric or "cosine").lower() == "cosine":
        return max(0.0, min(2.0, effective))
    return max(0.0, effective)


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
                first_result_list(closet_results, "documents"),
                first_result_list(closet_results, "metadatas"),
                first_result_list(closet_results, "distances"),
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
    metric: str = "cosine",
) -> list[dict[str, Any]]:
    closet_rank_boosts = [0.40, 0.25, 0.15, 0.08, 0.04]
    closet_distance_cap = 1.5

    scored: list[dict[str, Any]] = []
    for doc, meta, dist in zip(
        first_result_list(drawer_results, "documents"),
        first_result_list(drawer_results, "metadatas"),
        first_result_list(drawer_results, "distances"),
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
        effective_dist = _apply_distance_boost(float(dist), boost, metric)
        scored.append(
            {
                "text": doc,
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(source).name if source else "?",
                "distance": round(float(dist), 4),
                "similarity": round(_distance_to_similarity(effective_dist, metric), 3),
                "_sort_key": effective_dist,
                "metadata": meta,
            }
        )

    scored.sort(key=lambda h: h["_sort_key"])
    trimmed = scored[:n_results]
    for h in trimmed:
        h.pop("_sort_key", None)
    return trimmed
