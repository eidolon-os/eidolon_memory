"""Canonical exact-fact identity and provenance registration results."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field

from eidolon.memory.support.model_base import BaseEidolonModel


class CanonicalEvidenceConflict(RuntimeError):
    """One intent id was reused for a different canonical assertion."""


ProjectionTarget = Literal["drawer", "kg"]


class CanonicalFactRegistration(BaseEidolonModel):
    assertion_id: str
    memory_space_id: str
    intent_id: str
    evidence_count: int = Field(ge=1)
    evidence_created: bool
    pending_targets: list[ProjectionTarget] = Field(default_factory=list)


def canonical_assertion_id(
    memory_space_id: str,
    subject: str,
    predicate: str,
    object_: str,
) -> str:
    raw = "\x1f".join((memory_space_id, subject, predicate, object_))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"fact:{digest}"
