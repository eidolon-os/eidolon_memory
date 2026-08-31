"""Canonical exact-fact identity and provenance registration results."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Literal

from eidolon_memory_contracts import validate_audience
from pydantic import Field

from eidolon.memory.support.model_base import BaseEidolonModel


class CanonicalEvidenceConflict(RuntimeError):
    """One intent id was reused for a different canonical assertion."""


class CanonicalFactInactive(RuntimeError):
    """New evidence cannot implicitly reactivate an inactive assertion."""


class CanonicalFactConflict(RuntimeError):
    """A write would violate a product-defined canonical fact invariant."""


ProjectionTarget = Literal["drawer", "kg"]
CanonicalFactState = Literal["active", "invalidated", "superseded", "forgotten"]


@dataclass(frozen=True, slots=True)
class CanonicalFactStats:
    assertions_total: int
    assertions_active: int
    assertions_invalidated: int
    assertions_superseded: int
    assertions_forgotten: int
    evidence_total: int
    invalidations_total: int
    supersessions_total: int
    reactivations_total: int
    reactivations_pending: int
    invalidations_pending: int
    supersessions_pending: int
    forget_projections_pending: int
    drawer_not_projected: int
    drawer_projected: int
    kg_not_projected: int
    kg_projected: int
    database_bytes: int
    last_materialized_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class CanonicalForgetPlan(BaseEidolonModel):
    """Durable fact tombstone plus the source decisions that must be erased."""

    memory_space_id: str
    assertion_id: str
    source_event_ids: list[str] = Field(default_factory=list)
    hard: bool


class CanonicalFactRegistration(BaseEidolonModel):
    assertion_id: str
    memory_space_id: str
    intent_id: str
    evidence_count: int = Field(ge=1)
    evidence_created: bool
    state: CanonicalFactState = "active"
    pending_targets: list[ProjectionTarget] = Field(default_factory=list)
    projection_id: str | None = None
    reactivation_pending: bool = False


class CanonicalFactInvalidation(BaseEidolonModel):
    assertion_id: str
    memory_space_id: str
    intent_id: str
    matched: bool
    invalidation_created: bool = False
    invalidation_count: int = Field(ge=0, default=0)
    state: Literal["pending", "applied"] | None = None
    result_state: Literal["invalidated", "superseded"] = "invalidated"
    projection_id: str | None = None


class CanonicalFactRecord(BaseEidolonModel):
    assertion_id: str
    memory_space_id: str
    audience: str
    subject: str
    predicate: str
    object: str
    state: CanonicalFactState
    projection_id: str


class CanonicalFactEvidenceRecord(BaseEidolonModel):
    intent_id: str
    source_event_id: str
    authority: str
    raw_claim: str
    confidence: float
    occurred_at: str | None = None
    recorded_at: str


class CanonicalFactTransitionRecord(BaseEidolonModel):
    intent_id: str
    transition: Literal["invalidated", "superseded", "reactivated"]
    occurred_at: str
    recorded_at: str
    reason: str = ""
    from_state: CanonicalFactState | None = None
    to_state: CanonicalFactState


class CanonicalFactHistoryRecord(BaseEidolonModel):
    fact: CanonicalFactRecord
    created_at: str
    updated_at: str
    last_confirmed_at: str
    evidence: list[CanonicalFactEvidenceRecord] = Field(default_factory=list)
    transitions: list[CanonicalFactTransitionRecord] = Field(default_factory=list)
    evidence_capped: bool = False
    transitions_capped: bool = False


def canonical_assertion_id(
    memory_space_id: str,
    audience: str,
    subject: str,
    predicate: str,
    object_: str,
) -> str:
    raw = "\x1f".join((memory_space_id, audience, subject, predicate, object_))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"fact:{digest}"


def canonical_intent_audience(intent: object) -> str:
    attributes = getattr(intent, "attributes", {})
    raw = str(attributes.get("audience") or "").strip()
    if not raw:
        raise ValueError("canonical fact intent requires an explicit audience")
    return validate_audience(raw)
