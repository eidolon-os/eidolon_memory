"""Durable steward decision identity for deterministic projection retries."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from eidolon_memory_contracts import ConversationTurnPayload, MemoryIntent
from pydantic import Field

from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.support.model_base import BaseEidolonModel


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def extraction_input_hash(turn: ConversationTurnPayload) -> str:
    """Hash the validated turn deterministically, independent of wire formatting."""
    raw = json.dumps(
        turn.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
