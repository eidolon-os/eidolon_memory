"""Deterministic test-only turn projection.

This is not a semantic extractor and cannot be enabled in production. It lets
subprocess E2E tests exercise NATS, Chroma, recall and lifecycle plumbing without
networking to an LLM or reintroducing keyword classification.
"""

from __future__ import annotations

from eidolon_memory_contracts import ConversationTurnPayload

from eidolon.memory.application.steward.common import finalize_fragments
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.steward import StewardDecision


class VerbatimTestSteward:
    def __init__(self, settings: MemorySettings) -> None:
        self._settings = settings

    @property
    def extraction_version(self) -> str:
        return "test-verbatim:v1"

    async def aclose(self) -> None:
        pass

    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        text = turn.user_text.strip()
        if not text:
            return StewardDecision(should_write=False).stamped_by(
                self.extraction_version
            )
        fragment = MemoryFragment(
            memory_space_id=turn.context.memory_space_id,
            source_turn_id=turn.turn_id,
            wing="Wing_Life",
            room="test_turns",
            content=text,
            evidence_quote=text,
            memory_type="test",
            importance=max(3, self._settings.steward.min_importance_to_write),
            confidence=1.0,
        )
        fragments = finalize_fragments(
            [fragment],
            steward="test-verbatim",
            context=turn.context,
            source_turn_id=turn.turn_id,
        )
        return StewardDecision(
            should_write=True,
            fragments=fragments,
        ).stamped_by(self.extraction_version)
