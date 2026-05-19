"""NATS subject constants (D1 + KG plan §3.4 per-user subject hierarchy).

Subject layout::

  agent.memory.conversation.turn.<user_id>    ConversationTurnPayload
  agent.memory.cmd.<user_id>                  MemoryCommandPayload (KG writes etc.)

JetStream binds the stream to both wildcard patterns. agent_runner pull-subscribes
to one or the other depending on payload type.
"""

from __future__ import annotations

from eidolon.memory.config.palace_directory import validate_user_id


class SharedSubjects:
    """Memory-related bus subjects."""

    MEMORY_CONVERSATION_TURN_BASE = "agent.memory.conversation.turn"
    MEMORY_COMMAND_BASE = "agent.memory.cmd"


def conversation_turn_subject(user_id: str) -> str:
    """Return the per-user JetStream subject for a conversation turn write."""
    return (
        f"{SharedSubjects.MEMORY_CONVERSATION_TURN_BASE}.{validate_user_id(user_id)}"
    )


def conversation_turn_stream_pattern() -> str:
    """Wildcard subject (one of the patterns the JetStream stream binds to)."""
    return f"{SharedSubjects.MEMORY_CONVERSATION_TURN_BASE}.>"


def memory_command_subject(user_id: str) -> str:
    """Per-user subject for admin / agent commands (KG writes, delete, etc.)."""
    return f"{SharedSubjects.MEMORY_COMMAND_BASE}.{validate_user_id(user_id)}"


def memory_command_stream_pattern() -> str:
    """Wildcard subject for the command stream binding."""
    return f"{SharedSubjects.MEMORY_COMMAND_BASE}.>"


def all_stream_patterns() -> list[str]:
    """Subjects the JetStream stream must bind. Order: turn first, cmd second."""
    return [
        conversation_turn_stream_pattern(),
        memory_command_stream_pattern(),
    ]
