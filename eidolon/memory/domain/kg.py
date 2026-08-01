"""Knowledge Graph domain types: predicates, triples, commands.

The whitelist of predicates is enforced via :data:`KgPredicate` (a typing
``Literal``); LLM steward output that uses anything else gets rejected by
pydantic and the whole decision is dropped (see plan §4.2). This is the
project's defence against KG predicate sprawl from LLM hallucination.
"""

from __future__ import annotations

from eidolon_memory_contracts import KgPredicate as _KgPredicate
from pydantic import Field

from eidolon.memory.support.model_base import BaseEidolonModel

__all__ = [
    "KgEntityRecord",
    "KgInvalidationAction",
    "KgTripleAction",
    "KgTripleRecord",
]

# ── Predicate whitelist (KG plan §4.2) ──────────────────────────────────────


# ── Steward output schemas (parsed from LLM) ────────────────────────────────


class KgTripleAction(BaseEidolonModel):
    """One ``add_triple`` instruction emitted by the LLM steward."""

    subject: str = Field(min_length=1, max_length=128)
    predicate: _KgPredicate
    object: str = Field(min_length=1, max_length=256)
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.9)


class KgInvalidationAction(BaseEidolonModel):
    """Steward output: stop a previously-valid triple at ``ended`` (or NOW)."""

    subject: str = Field(min_length=1)
    predicate: _KgPredicate
    object: str = Field(min_length=1)
    ended: str | None = None
    reason: str = Field(default="", max_length=128)


# ── Read-side records (returned by the graph) ────────────────────


class KgEntityRecord(BaseEidolonModel):
    """One entity returned by KG queries."""

    id: str
    name: str
    type: str = "unknown"


class KgTripleRecord(BaseEidolonModel):
    """One triple returned by KG queries (already resolved to display names)."""

    id: str
    subject: str
    predicate: str
    object: str
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = 1.0
    source_turn_id: str | None = None
    adapter_name: str | None = None
