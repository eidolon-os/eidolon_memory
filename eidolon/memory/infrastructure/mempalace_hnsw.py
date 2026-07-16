"""Preflight MemPalace's Chroma HNSW index before any vector segment open."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class HnswSafetyStatus:
    """Normalized result of MemPalace's non-throwing capacity probe."""

    status: str
    vector_disabled: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def probe_hnsw_safety(
    palace_path: str,
    collection_name: str | None = None,
) -> HnswSafetyStatus:
    """Return whether vector access must be disabled for a Chroma palace.

    Only a confirmed divergence disables vectors.  An inconclusive probe stays
    fail-open, matching MemPalace's MCP behavior, while preserving the reason
    for logs and status surfaces.  The caller must invoke this before opening a
    collection because the unsafe failure mode can occur in native HNSW load.
    """

    try:
        from mempalace.backends.chroma import hnsw_capacity_status

        if collection_name:
            raw = hnsw_capacity_status(palace_path, collection_name)
        else:
            raw = hnsw_capacity_status(palace_path)
    except Exception as exc:  # pragma: no cover - exercised through monkeypatch
        return HnswSafetyStatus(
            status="unknown",
            vector_disabled=False,
            message=f"HNSW capacity probe failed: {exc}",
        )

    details = dict(raw) if isinstance(raw, dict) else {}
    status = str(details.get("status") or "unknown").strip().lower()
    diverged = bool(details.get("diverged")) or status == "diverged"
    return HnswSafetyStatus(
        status=status,
        vector_disabled=diverged,
        message=str(details.get("message") or ""),
        details=details,
    )
