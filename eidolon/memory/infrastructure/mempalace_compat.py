"""Version-scoped compatibility helpers for MemPalace internals.

Only this module may import private MemPalace symbols.  Callers get stable,
small helpers with local fallbacks so an upstream internal rename is caught
by the adapter contract suite instead of being scattered through hot paths.
"""

from __future__ import annotations

import math
from typing import Any


def collection_metric(collection: Any) -> str:
    """Return a supported distance metric for a MemPalace collection."""

    try:
        from mempalace.searcher import _metric_for_collection

        return str(_metric_for_collection(collection))
    except Exception:
        try:
            metric = getattr(collection, "distance_metric", "cosine")
        except Exception:
            return "cosine"
        metric = str(metric or "cosine").lower()
        return metric if metric in {"cosine", "l2", "ip"} else "cosine"


def distance_similarity(distance: float | None, metric: str = "cosine") -> float:
    """Convert an upstream distance to a stable, higher-is-better score."""

    try:
        from mempalace.searcher import _distance_to_similarity

        return float(_distance_to_similarity(distance, metric))
    except Exception:
        if distance is None:
            return 0.0
        metric = (metric or "cosine").lower()
        if metric == "l2":
            return 1.0 / (1.0 + max(0.0, float(distance)))
        if metric == "ip":
            return 1.0 / (1.0 + math.exp(min(60.0, float(distance))))
        return max(0.0, 1.0 - float(distance))


def first_result_list(results: Any, key: str) -> list[Any]:
    """Read the first Chroma result batch, tolerating partial payloads."""

    try:
        from mempalace.searcher import _first_or_empty

        return list(_first_or_empty(results, key))
    except Exception:
        if isinstance(results, dict):
            values = results.get(key)
        else:
            values = getattr(results, key, None)
        if not values:
            return []
        first = values[0]
        return list(first) if isinstance(first, (list, tuple)) else []
