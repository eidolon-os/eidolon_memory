"""Canonical exact-fact identity and provenance registration results."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import Field

from eidolon.memory.support.model_base import BaseEidolonModel


class CanonicalEvidenceConflict(RuntimeError):
    """One intent id was reused for a different canonical assertion."""


class CanonicalFactInactive(RuntimeError):
    """New evidence cannot implicitly reactivate an invalidated assertion."""


ProjectionTarget = Literal["drawer", "kg"]
CanonicalFactState = Literal["active", "invalidated"]


@dataclass(frozen=True, slots=True)
class CanonicalFactStats:
    assertions_total: int
    assertions_active: int
    assertions_invalidated: int
    evidence_total: int
    invalidations_total: int
    invalidations_pending: int
    drawer_not_projected: int
    drawer_projected: int
    kg_not_projected: int
    kg_projected: int
    database_bytes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class CanonicalFactRegistration(BaseEidolonModel):
    assertion_id: str
    memory_space_id: str
    intent_id: str
    evidence_count: int = Field(ge=1)
    evidence_created: bool
    state: CanonicalFactState = "active"
    pending_targets: list[ProjectionTarget] = Field(default_factory=list)


class CanonicalFactInvalidation(BaseEidolonModel):
    assertion_id: str
    memory_space_id: str
    intent_id: str
    matched: bool
    invalidation_created: bool = False
    invalidation_count: int = Field(ge=0, default=0)
    state: Literal["pending", "applied"] | None = None


def canonical_assertion_id(
    memory_space_id: str,
    subject: str,
    predicate: str,
    object_: str,
) -> str:
    raw = "\x1f".join((memory_space_id, subject, predicate, object_))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"fact:{digest}"
