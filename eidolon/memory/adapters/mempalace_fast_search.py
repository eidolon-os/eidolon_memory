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
        from mempalace.searcher import build_where_filter
    except ImportError as exc:
        raise MemoryBackendUnavailable("mempalace package is not installed") from exc

    embedding = query_embedding if query_embedding is not None else embed_query_vector(query)
    vec = [embedding]

    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_hits: list[dict[str, Any]] = []
    workers = max(1, min(len(wings), 4))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _search_one_wing,
                palace_path,
                query,
                wing_id,
                room,
                n_results,
                query_embeddings=vec,
                skip_closets=skip_closets,
                collection_name=collection_name,
            ): wing_id
            for wing_id in wings
        }
        for fut in as_completed(futures):
            try:
                all_hits.extend(fut.result())
            except Exception as exc:
                raise MemoryBackendUnavailable(str(exc)) from exc

    all_hits.sort(key=lambda h: float(h.get("similarity", 0.0)), reverse=True)
    return all_hits[: max(n_results * len(wings), n_results)]


def _search_one_wing(
    palace_path: str,
    query: str,
    wing: str,
    room: str | None,
    n_results: int,
    *,
    query_embeddings: list[list[float]],
    skip_closets: bool,
    collection_name: str | None,
) -> list[dict[str, Any]]:
    from mempalace.palace import get_collection
    from mempalace.searcher import _first_or_empty, build_where_filter

    drawers_col = get_collection(palace_path, collection_name=collection_name, create=False)
    where = build_where_filter(wing, room)
    dkwargs: dict[str, Any] = {
        "query_embeddings": query_embeddings,
        "n_results": max(n_results * 3, n_results),
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        dkwargs["where"] = where

    drawer_results = drawers_col.query(**dkwargs)

    closet_boost_by_source: dict[str, tuple] = {}
    if not skip_closets:
        try:
            from mempalace.palace import get_closets_collection

            closets_col = get_closets_collection(palace_path, create=False)
            ckwargs: dict[str, Any] = {
                "query_embeddings": query_embeddings,
                "n_results": n_results * 2,
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                ckwargs["where"] = where
            closet_results = closets_col.query(**ckwargs)
            for rank, (cdoc, cmeta, cdist) in enumerate(
                zip(
                    _first_or_empty(closet_results, "documents"),
                    _first_or_empty(closet_results, "metadatas"),
                    _first_or_empty(closet_results, "distances"),
                )
            ):
                cmeta = cmeta or {}
                source = cmeta.get("source_file", "")
                if source and source not in closet_boost_by_source:
                    closet_boost_by_source[source] = (rank, cdist, (cdoc or "")[:200])
        except Exception:
            pass

    closet_rank_boosts = [0.40, 0.25, 0.15, 0.08, 0.04]
    closet_distance_cap = 1.5

    scored: list[dict[str, Any]] = []
    for doc, meta, dist in zip(
        _first_or_empty(drawer_results, "documents"),
        _first_or_empty(drawer_results, "metadatas"),
        _first_or_empty(drawer_results, "distances"),
    ):
        meta = meta or {}
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
                "wing": meta.get("wing", wing),
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
