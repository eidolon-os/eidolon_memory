"""Normalize vector scores and rank recall hits for MCP/LiveKit responses."""

from __future__ import annotations

from typing import Any

from eidolon.memory.domain.wire import MemoryWireRecord

# Stripped from API payloads; kept on MemoryWireRecord.metadata for debugging only.
_INTERNAL_METADATA_KEYS = frozenset(
    {
        "score",
        "distance",
        "effective_distance",
        "closet_boost",
        "matched_via",
        "_distance",
        "_effective_distance",
        "_sort_key",
        "_source_file_full",
        "_chunk_index",
    }
)


def vector_fields_from_hit(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Return (similarity for ranking/API, internal-only metadata fields)."""
    dist = row.get("distance")
    sim = row.get("similarity")
    if sim is not None:
        similarity = float(sim)
    elif dist is not None:
        similarity = max(0.0, 1.0 - float(dist))
    else:
        legacy = row.get("score")
        if legacy is not None:
            # Historical bug: score sometimes held distance; treat as distance if > 1.
            val = float(legacy)
            similarity = max(0.0, 1.0 - val) if val > 1.0 else val
        else:
            similarity = 0.0

    internal: dict[str, Any] = {}
    if dist is not None:
        internal["_distance"] = float(dist)
    eff = row.get("effective_distance")
    if eff is not None:
        internal["_effective_distance"] = float(eff)
    return similarity, internal


def record_similarity(rec: MemoryWireRecord) -> float:
    raw = rec.metadata.get("similarity")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    dist = rec.metadata.get("_distance", rec.metadata.get("distance"))
    if dist is not None:
        try:
            return max(0.0, 1.0 - float(dist))
        except (TypeError, ValueError):
            pass
    return 0.0


def rank_records_by_similarity(
    hits: list[MemoryWireRecord],
    *,
    top_k: int,
) -> list[MemoryWireRecord]:
    """Global re-rank after multi-wing merge (O(n log n), n ≈ wings × per-wing top_k)."""
    ordered = sorted(hits, key=record_similarity, reverse=True)
    return ordered[: max(1, top_k)]


def public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Expose only caller-facing metadata (similarity, wing, room, …)."""
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in _INTERNAL_METADATA_KEYS or str(key).startswith("_"):
            continue
        out[key] = value
    sim = metadata.get("similarity")
    if sim is not None and "similarity" not in out:
        try:
            out["similarity"] = float(sim)
        except (TypeError, ValueError):
            out["similarity"] = sim
    elif "_distance" in metadata and "similarity" not in out:
        try:
            out["similarity"] = max(0.0, 1.0 - float(metadata["_distance"]))
        except (TypeError, ValueError):
            pass
    return out
