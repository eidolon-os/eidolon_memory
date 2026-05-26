"""Structured outputs for memory steward implementations."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
from eidolon.memory.support.model_base import BaseEidolonModel

PrivacyActionName = Literal["do_not_store", "archive_topic", "delete_request"]


class PrivacyAction(BaseEidolonModel):
    """A privacy-first action requested or implied by the user."""

    action: PrivacyActionName
    target: str
    reason: str


class EntityMention(BaseEidolonModel):
    """A user-spoken reference to an entity, paired with its canonical id.

    Phase 3 — bridges natural-language references ("妈妈" / "我家狗") to the
    KG's canonical entity ids (``mother:张丽`` / ``pet:铁锤``) so recall can
    hit on aliases without expensive LLM-side query rewriting.

    Invariants enforced at write time (turn_processor §3d, anti-hallucination):
      - ``entity_id`` MUST appear as a subject or object in the same turn's
        ``StewardDecision.triples``; mentions referencing facts that weren't
        also asserted in the same turn are rejected.

    ``confidence`` rubric per the prompt:
      - 0.95 — explicit kinship/称谓 ("我妈", "我老婆")
      - 0.85 — category/类别 ("我家狗", "公司")
      - 0.70 — pronoun/代词 ("她", "他", "它")
    """

    entity_id: str
    alias: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.85)


class StewardDecision(BaseEidolonModel):
    """The steward's write decision for one conversation turn.

    KG plan §4.2: ``triples`` and ``invalidations`` are populated by the
    LiteLLM steward when the user's turn provides clear, ground-truth facts
    or explicit change-of-mind. The rule-based steward leaves them empty.

    Phase 3 adds ``mentions``: pydantic default ``[]`` keeps JetStream replay
    safe on payloads from older runs that didn't carry the field.
    """

    should_write: bool
    reason: str = ""
    fragments: list[MemoryFragment] = Field(default_factory=list)
    triples: list[KgTripleAction] = Field(default_factory=list)
    invalidations: list[KgInvalidationAction] = Field(default_factory=list)
    privacy_actions: list[PrivacyAction] = Field(default_factory=list)
    mentions: list[EntityMention] = Field(default_factory=list)
