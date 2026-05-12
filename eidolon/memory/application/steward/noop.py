"""Default steward: persist the raw turn as one ingest blob (no extra LLM)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eidolon.memory.application.ingest import ingest_fragment
from eidolon.memory.domain.payloads import ConversationTurnPayload

if TYPE_CHECKING:
    from eidolon.memory.domain.ports import MemoryBackend


class NoOpSteward:
    """MVP worker handler — structured blob suitable for later LLM-based stewards."""

    async def handle_turn(self, turn: ConversationTurnPayload, backend: MemoryBackend) -> None:
        wing = turn.user_id or "default"
        room = turn.session_id or "general"
        text = f"[USER]: {turn.user_text}\n[ASSISTANT]: {turn.assistant_text}"
        await ingest_fragment(
            backend,
            wing=wing,
            room=room,
            text=text,
            metadata=turn.metadata or {},
            serialize_lock=None,
        )
