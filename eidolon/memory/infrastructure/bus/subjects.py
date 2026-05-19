"""NATS subject constants (D1: per-user subject hierarchy).

Subject layout:

* base: ``agent.memory.conversation.turn``
* per-user: ``agent.memory.conversation.turn.<user_id>``
* stream binding: ``agent.memory.conversation.turn.>``
"""

from __future__ import annotations

from eidolon.memory.config.palace_directory import validate_user_id


class SharedSubjects:
    """Memory-related bus subjects (D1: only ConversationTurn survives)."""

    MEMORY_CONVERSATION_TURN_BASE = "agent.memory.conversation.turn"


def conversation_turn_subject(user_id: str) -> str:
    """Return the per-user JetStream subject for a conversation turn write."""
    return (
        f"{SharedSubjects.MEMORY_CONVERSATION_TURN_BASE}.{validate_user_id(user_id)}"
    )


def conversation_turn_stream_pattern() -> str:
    """Wildcard subject for the JetStream stream binding."""
    return f"{SharedSubjects.MEMORY_CONVERSATION_TURN_BASE}.>"
