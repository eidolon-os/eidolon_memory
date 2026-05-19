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


class StewardDecision(BaseEidolonModel):
    """The steward's write decision for one conversation turn.

    KG plan §4.2: ``triples`` and ``invalidations`` are populated by the
    LiteLLM steward when the user's turn provides clear, ground-truth facts
    or explicit change-of-mind. The rule-based steward leaves them empty.
    """

    should_write: bool
    reason: str = ""
    fragments: list[MemoryFragment] = Field(default_factory=list)
    triples: list[KgTripleAction] = Field(default_factory=list)
    invalidations: list[KgInvalidationAction] = Field(default_factory=list)
    privacy_actions: list[PrivacyAction] = Field(default_factory=list)
