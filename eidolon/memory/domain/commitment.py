"""Deterministic commitment identity, revisions and lifecycle records."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Literal

from pydantic import Field

from eidolon.memory.support.model_base import BaseEidolonModel

CommitmentStatus = Literal[
    "proposed",
    "confirmed",
    "fulfilled",
    "cancelled",
    "superseded",
]
ACTIVE_COMMITMENT_STATUSES = frozenset({"proposed", "confirmed"})
TERMINAL_COMMITMENT_STATUSES = frozenset(
    {"fulfilled", "cancelled", "superseded"}
)
COMMITMENT_PREDICATES = frozenset({"promised", "committed_to", "planned_to"})


class CommitmentConflict(RuntimeError):
    """An intent conflicts with an existing commitment or revision."""


class CommitmentRecord(BaseEidolonModel):
    commitment_id: str
    memory_space_id: str
    promisor: str
    predicate: str
    action: str
    beneficiaries: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    condition: str | None = None
    due_at: str | None = None
    status: CommitmentStatus
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str
    drawer_projection_state: Literal["pending", "projected"] = "pending"
    kg_projection_state: Literal["pending", "projected"] = "pending"


class CommitmentRevisionRecord(BaseEidolonModel):
    revision_id: str
    commitment_id: str
    intent_id: str
    source_event_id: str
    authority: str
    operation: str
    previous_status: CommitmentStatus | None = None
    status: CommitmentStatus
    raw_claim: str
    snapshot: CommitmentRecord
    recorded_at: str


class CommitmentApplyResult(BaseEidolonModel):
    commitment: CommitmentRecord
    revision: CommitmentRevisionRecord
    commitment_created: bool
    revision_created: bool


class CommitmentListPage(BaseEidolonModel):
    """One bounded current read with an exact same-snapshot total."""

    commitments: list[CommitmentRecord] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    truncated: bool


def normalize_commitment_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


def commitment_identity(
    memory_space_id: str,
    promisor: str,
    predicate: str,
    action: str,
    beneficiaries: list[str],
) -> str:
    payload = json.dumps(
        {
            "memory_space_id": memory_space_id,
            "promisor": normalize_commitment_text(promisor),
            "predicate": normalize_commitment_text(predicate),
            "action": normalize_commitment_text(action),
            "beneficiaries": sorted(
                {normalize_commitment_text(item) for item in beneficiaries}
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "commitment:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
