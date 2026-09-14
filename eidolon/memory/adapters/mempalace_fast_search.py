"""Voice-optimized search: one query embedding, many wings (no re-embed per wing)."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mempalace.backends.base import QueryResult

from eidolon.memory.adapters.mempalace_query_embedding import embed_query_vector
from eidolon.memory.adapters.mempalace_results import storage_record
from eidolon.memory.domain.errors import MemoryBackendUnavailable
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.mempalace_hnsw import probe_hnsw_safety


def search_memories_shared_embedding(
    query: str,
    palace_path: str,
    *,
    wings: list[str],
    room: str | None,
    audiences: tuple[str, ...] | None = None,
    #: The caller's device, so ``current_device`` rows it cannot be shown
    #: are excluded by the query instead of after it. See
    #: ``_device_visibility_filter``.
    device_id: str | None = None,
    n_results: int,
    query_embedding: list[float] | None = None,
    skip_closets: bool = True,
    collection_name: str | None = None,
    diagnostics: dict[str, float] | None = None,
    memory_space_id: str | None = None,
) -> list[MemoryWireRecord]:
    """Search multiple wings using a single precomputed query embedding."""
    if n_results <= 0:
        return []
    total_started = time.perf_counter()
    probe_started = time.perf_counter()
    hnsw_safety = probe_hnsw_safety(palace_path, collection_name)
    _record_ms(diagnostics, "hnsw_probe_ms", probe_started)
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

    embedding_started = time.perf_counter()
    if query_embedding is None:
        embedding = embed_query_vector(query)
    else:
        embedding = query_embedding
    vec = [embedding]
    _record_ms(diagnostics, "embedding_ms", embedding_started)

    try:
        open_started = time.perf_counter()
        drawers_col = get_collection(
            palace_path,
            collection_name=collection_name,
            create=False,
            read_only=True,
        )
        _record_ms(diagnostics, "storage_open_ms", open_started)
        metric = str(drawers_col.distance_metric)
        where = _combined_where(wings, room, audiences, device_id)
        limit = max(n_results * max(3, len(wings) * 3), n_results)
        query_started = time.perf_counter()
        drawer_results = _query_collection(
            drawers_col,
            query_embeddings=vec,
            n_results=limit,
            where=where,
        )
        _record_ms(diagnostics, "chroma_query_ms", query_started)

        closet_boost_by_source = {}
        if not skip_closets:
            closet_started = time.perf_counter()
            closet_boost_by_source = _closet_boosts(
                palace_path,
                query_embeddings=vec,
                n_results=n_results,
                where=where,
            )
            _record_ms(diagnostics, "closet_query_ms", closet_started)
    except Exception as exc:
        raise MemoryBackendUnavailable(str(exc)) from exc

    scoring_started = time.perf_counter()
    hits = _score_results(
        drawer_results,
        n_results=max(n_results * len(wings), n_results),
        closet_boost_by_source=closet_boost_by_source,
        metric=metric,
        memory_space_id=memory_space_id,
    )
    _record_ms(diagnostics, "storage_rank_ms", scoring_started)
    _record_ms(diagnostics, "vector_storage_total_ms", total_started)
    return hits[: max(n_results * len(wings), n_results)]


def _record_ms(diagnostics: dict[str, float] | None, key: str, started: float) -> None:
    if diagnostics is not None:
        diagnostics[key] = round((time.perf_counter() - started) * 1000, 3)


def _search_sqlite_fallback(
    query: str,
    palace_path: str,
    *,
    wings: list[str],
    room: str | None,
    n_results: int,
    collection_name: str | None,
) -> list[MemoryWireRecord]:
    """Probe SQLite without unsafe HNSW access; 3.9 cannot verify nonempty hits' visibility."""
    try:
        from mempalace.searcher import search_memories

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
            if payload.get("error"):
                raise MemoryBackendUnavailable(str(payload["error"]))
            if payload.get("results"):
                # 3.9's public fallback strips permission metadata. A broad metadata
                # scan or private SQL hydration would introduce a second read path.
                raise MemoryBackendUnavailable(
                    "MemPalace SQLite fallback omitted visibility metadata"
                )
    except MemoryBackendUnavailable:
        raise
    except Exception as exc:
        raise MemoryBackendUnavailable(str(exc)) from exc
    return []


def _device_visibility_filter(device_id: str | None) -> dict[str, Any]:
    """Rows this caller could actually be shown, as a query predicate.

    Audience was already pushed into the query and device visibility was not,
    so ``current_device`` rows were fetched into the ``n_results`` window and
    then dropped by ``recall_policy``. A population of them therefore starves
    recall of everything else: a palace with 40 such drawers beside 38 visible
    ones returned one hit where the 38 alone returned five.

    That is not hypothetical and not new — the steward writes
    ``visibility=current_device`` for device-local facts, so the read path has
    always had this, only at a smaller scale.

    Deliberately only subtractive of what the post-filter would drop anyway:
    ``private`` is left entirely to ``recall_policy``, which stays the
    authority. This exists so invisible rows do not spend slots, not to decide
    visibility in two places.

    ``visibility``, ``source_device_id`` and ``target_device_id`` are stamped
    unconditionally on every drawer — empty strings when absent — so ``$ne``
    cannot silently exclude a row for lacking the key.
    """

    device = str(device_id or "").strip()
    if not device:
        # No device means ``current_device`` can never match, so the whole
        # class is unreachable for this caller.
        return {"visibility": {"$ne": "current_device"}}
    return {
        "$or": [
            {"visibility": {"$ne": "current_device"}},
            {"source_device_id": device},
            {"target_device_id": device},
        ]
    }


def _combined_where(
    wings: list[str],
    room: str | None,
    audiences: tuple[str, ...] | None,
    device_id: str | None = None,
) -> dict[str, Any] | None:
    if not wings and not room and audiences is None and device_id is None:
        return None
    wing_filter: dict[str, Any] | None
    if not wings:
        wing_filter = None
    elif len(wings) == 1:
        wing_filter = {"wing": wings[0]}
    else:
        wing_filter = {"wing": {"$in": wings}}
    filters = [item for item in (wing_filter, {"room": room} if room else None) if item]
    if audiences is not None:
        filters.append({"audience": {"$in": list(audiences)}})
        # Paired with the audience clause on purpose: both answer "may this
        # caller be shown this row", and pushing one down while post-filtering
        # the other is what let invisible rows spend the window.
        filters.append(_device_visibility_filter(device_id))
    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return {"$and": filters}


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
    """Normalize the public collection distance into the Agent score contract."""

    if distance is None:
        return 0.0
    if metric == "l2":
        return 1.0 / (1.0 + max(0.0, float(distance)))
    if metric == "ip":
        return 1.0 / (1.0 + math.exp(min(60.0, float(distance))))
    return max(0.0, 1.0 - float(distance))


def _first_batch(result: Any, field: str) -> list[Any]:
    """Read one query batch from MemPalace's public typed result."""

    batches = getattr(result, field)
    if len(batches) != 1:
        if not batches and not result.ids:
            return []
        raise MemoryBackendUnavailable("expected one MemPalace query batch")
    return list(batches[0])


def _apply_distance_boost(distance: float, boost: float, metric: str) -> float:
    effective = float(distance) - boost
    if (metric or "cosine").lower() == "cosine":
        return max(0.0, min(2.0, effective))
    return max(0.0, effective)


def _closet_boosts(
    palace_path: str,
    *,
    query_embeddings: list[list[float]],
    n_results: int,
    where: dict[str, Any] | None,
) -> dict[str, tuple]:
    try:
        from mempalace.palace import get_collection

        closets_col = get_collection(
            palace_path,
            collection_name="mempalace_closets",
            create=False,
            read_only=True,
        )
        closet_results = _query_collection(
            closets_col,
            query_embeddings=query_embeddings,
            n_results=n_results * 2,
            where=where,
        )

        out: dict[str, tuple] = {}
        for rank, (cdoc, cmeta, cdist) in enumerate(
            zip(
                _first_batch(closet_results, "documents"),
                _first_batch(closet_results, "metadatas"),
                _first_batch(closet_results, "distances"),
            )
        ):
            cmeta = cmeta or {}
            source = cmeta.get("source_file", "")
            if source and source not in out:
                out[source] = (rank, cdist, (cdoc or "")[:200])
        return out
    except Exception:
        return {}


def _score_results(
    drawer_results: QueryResult,
    *,
    n_results: int,
    closet_boost_by_source: dict[str, tuple],
    metric: str = "cosine",
    memory_space_id: str | None = None,
) -> list[MemoryWireRecord]:
    closet_rank_boosts = [0.40, 0.25, 0.15, 0.08, 0.04]
    closet_distance_cap = 1.5

    scored: list[MemoryWireRecord] = []
    batches = [
        _first_batch(drawer_results, field)
        for field in ("ids", "documents", "metadatas", "distances")
    ]
    if len({len(batch) for batch in batches}) != 1:
        raise MemoryBackendUnavailable("misaligned MemPalace query result")
    for drawer_id, doc, meta, dist in zip(*batches, strict=True):
        # These metadata came from the same Chroma query as the vector hit, not
        # from MemPalace's older lossy public search payload. Mark that provenance
        # explicitly so privacy filtering never needs a second hydration read.
        meta = {**(meta or {}), "_storage_metadata_verified": True}
        doc = doc or ""
        source = meta.get("source_file", "") or ""
        boost = 0.0
        if source in closet_boost_by_source:
            c_rank, c_dist, _preview = closet_boost_by_source[source]
            if c_dist <= closet_distance_cap and c_rank < len(closet_rank_boosts):
                boost = closet_rank_boosts[c_rank]
        effective_dist = _apply_distance_boost(float(dist), boost, metric)
        record = storage_record(
            drawer_id,
            doc,
            meta,
            memory_space_id=memory_space_id,
        )
        record.metadata.update(
            similarity=round(_distance_to_similarity(float(dist), metric), 3),
            _distance=float(dist),
            _effective_distance=effective_dist,
            _retrieval_score=_distance_to_similarity(effective_dist, metric),
        )
        scored.append(record)

    scored.sort(key=lambda r: r.metadata["_retrieval_score"], reverse=True)
    return scored[:n_results]
