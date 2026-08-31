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
    evidence_quote: str = ""


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
    or explicit change-of-mind. All durable actions come from the configured
    semantic steward; there is no keyword-based fallback authority.

    Phase 3 adds ``mentions``: pydantic default ``[]`` keeps JetStream replay
    safe on payloads from older runs that didn't carry the field.

    ``produced_by`` says which extractor actually produced this decision, and it
    is the only place that information exists. The configured policy version — the
    ledger's identity key — cannot carry it: that key is computed *before*
    ``decide`` runs, because it is what the idempotency lookup is keyed on, so it
    can only describe the policy in force, never the outcome. Collapsing the two
    meant a turn whose LLM call failed and fell back to rules was recorded as
    LLM-extracted, and therefore never re-extracted when the endpoint recovered:
    the memory stayed degraded and the ledger said otherwise.

    Stamped by the implementation that produced the decision, so a steward
    delegating to another needs no code at the delegation site — the returned
    decision already carries the right value. That is what makes the mistake
    unrepresentable rather than fixed.
    """

    should_write: bool
    """Whether the **fragments** are worth storing. Not the whole turn.

    Read as a global gate it looks like a bug that the graph writes when this is
    false, and it was filed as one. It is not, and gating on it would be a worse
    bug than the one it appears to fix:

    * ``invalidations`` would be dropped. A person correcting a fact — "不对，我妈
      搬到北京了" — produces an invalidation whether or not the same turn yields a
      fragment worth keeping, and ignoring the correction leaves the old fact
      recallable. Silently.
    * ``triples`` would be dropped whenever the turn's fragments fell below
      ``min_importance_to_write``. A low-importance, high-confidence relational
      fact is precisely what the graph is for and the vector store is not; the
      two have separate thresholds because they are separate judgements.

    The LLM steward sets this true exactly when fragments survive filtering
    (``llm.py``), and privacy actions deliberately run *before* the gate in
    ``turn_processor`` — both consistent with a fragment-scoped meaning and not
    with a turn-scoped one. Renaming it would touch the wire contract two other
    repos read, so the meaning is pinned here and in a test instead.
    """

    reason: str = ""
    #: Empty means "not recorded" — decisions replayed from before this field
    #: existed, and test fixtures that do not care. Readers must treat it as
    #: unknown rather than as a value.
    produced_by: str = ""
    fragments: list[MemoryFragment] = Field(default_factory=list)
    triples: list[KgTripleAction] = Field(default_factory=list)
    invalidations: list[KgInvalidationAction] = Field(default_factory=list)
    privacy_actions: list[PrivacyAction] = Field(default_factory=list)
    mentions: list[EntityMention] = Field(default_factory=list)

    def stamped_by(self, producer: str) -> StewardDecision:
        """Record ``producer`` as the extractor, unless one is already recorded.

        Never overwriting is the whole mechanism. A steward that falls back to
        another returns the other's decision, already stamped; if the outer
        steward's stamp won, the fallback would again be indistinguishable from a
        successful extraction — the exact defect this field exists to prevent.

        So each implementation stamps unconditionally at one place, and delegation
        stays honest without any code at the delegation site.
        """

        if self.produced_by:
            return self
        return self.model_copy(update={"produced_by": producer})
