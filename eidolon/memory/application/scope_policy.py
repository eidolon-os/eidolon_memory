"""One policy for deciding who may recall derived memory.

Raw interaction memory is narrow by default.  Owner-wide memory is a derived
projection reserved for stable, low-risk facts; it is never the accidental
default of a conversation write.
"""

from __future__ import annotations

from eidolon_memory_contracts import (
    OWNER_AUDIENCE,
    companion_audience,
    council_audience,
)

# These predicates describe durable owner facts that remain useful whichever
# Companion is active. Relationship history, promises, episodes, emotions and
# health stay in the interaction audience.
_OWNER_SHARED_PREDICATES = frozenset({
    "works_at",
    "lives_in",
    "studies_at",
    "holds_role",
    "born_in",
    "likes",
    "dislikes",
    "prefers",
    "does",
    "practices",
    "owns",
    "uses",
})


def interaction_audience(context: object) -> str:
    """Return the narrow audience of one turn."""

    council_id = str(getattr(context, "council_id", "") or "").strip()
    if council_id:
        return council_audience(council_id)
    companion_id = str(getattr(context, "companion_id", "") or "").strip()
    if companion_id:
        return companion_audience(companion_id)
    return OWNER_AUDIENCE


def derived_triple_audience(predicate: str, context: object) -> str:
    """Promote only stable, low-risk facts; keep all other triples narrow."""

    if predicate in _OWNER_SHARED_PREDICATES:
        return OWNER_AUDIENCE
    return interaction_audience(context)
