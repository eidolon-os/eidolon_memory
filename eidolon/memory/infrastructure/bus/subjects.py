"""NATS subject constants (D1 + KG plan §3.4 per-user subject hierarchy).

Subject layout::

  agent.memory.conversation.turn.<user_id>    ConversationTurnPayload
  agent.memory.cmd.<user_id>                  MemoryCommandPayload (KG writes etc.)

JetStream binds the stream to both wildcard patterns. agent_runner pull-subscribes
to one or the other depending on payload type.
"""

from __future__ import annotations

from eidolon_sdk.memory import (
    MEMORY_COMMAND_BASE,
    MEMORY_CONVERSATION_TURN_BASE,
    all_memory_stream_patterns,
    conversation_turn_stream_pattern,
    conversation_turn_subject,
    memory_command_stream_pattern,
    memory_command_subject,
)

__all__ = [
    "SharedSubjects",
    "all_stream_patterns",
    "conversation_turn_stream_pattern",
    "conversation_turn_subject",
    "memory_command_stream_pattern",
    "memory_command_subject",
]


class SharedSubjects:
    """Memory-related bus subjects."""

    MEMORY_CONVERSATION_TURN_BASE = MEMORY_CONVERSATION_TURN_BASE
    MEMORY_COMMAND_BASE = MEMORY_COMMAND_BASE


def all_stream_patterns() -> list[str]:
    """Subjects the JetStream stream must bind. Order: turn first, cmd second."""
    return all_memory_stream_patterns()
