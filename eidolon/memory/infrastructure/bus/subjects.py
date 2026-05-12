"""NATS subject constants for memory service (must match eidolon_daemon SharedSubjects)."""

from __future__ import annotations


class SharedSubjects:
    """Memory-related bus subjects — values must stay aligned with agent processes."""

    MEMORY_QUERY = "agent.memory.query"
    MEMORY_STORE = "agent.memory.store"
    MEMORY_RESULT = "agent.memory.result"
    MEMORY_GET = "agent.memory.get"
    MEMORY_GET_ALL = "agent.memory.get_all"
    MEMORY_DELETE = "agent.memory.delete"
    MEMORY_CONVERSATION_TURN = "agent.memory.conversation.turn"
