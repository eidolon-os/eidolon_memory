"""Durable steward decision identity for deterministic projection retries."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from eidolon_memory_contracts import ConversationTurnPayload, MemoryIntent
from pydantic import Field

from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.support.model_base import BaseEidolonModel

PROVISIONAL_SOURCE_VERSION = "provisional-source-v1"
# Sources kept, nothing derived: a candidate's provisional turns, and turns whose
# voice Data has not attributed yet (design §3.5).
RETAINED_ATTRIBUTIONS = frozenset({"provisional", "pending"})
DEFAULT_PROVISIONAL_MAX_RECORDS = 10_000
DEFAULT_PROVISIONAL_MAX_BYTES = 64 * 1024 * 1024


class ProvisionalSourceCapacityExceeded(RuntimeError):
    """New source cannot fit; existing sources must never be silently evicted."""


class ExtractionDecisionConflict(RuntimeError):
    """The same extraction identity was reused for different turn input."""


class ExtractionDecisionRecord(BaseEidolonModel):
    """One validated steward result, stored before any projection is attempted."""

    memory_space_id: str
    source_turn_id: str
    extractor_version: str
    input_hash: str
    decision: StewardDecision
    intents: list[MemoryIntent] = Field(default_factory=list)
    redacted: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def extraction_input_hash(turn: ConversationTurnPayload) -> str:
    """Hash the validated turn deterministically, independent of wire formatting."""
    payload = turn.model_dump(mode="json")
    # Preserve persisted pre-person hashes on legacy event retries.
    for key in ("person_id", "identity_epoch"):
        if payload["context"].get(key) is None:
            payload["context"].pop(key, None)
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
