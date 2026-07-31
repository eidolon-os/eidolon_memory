"""Memory-related NATS subject contracts."""

from __future__ import annotations

import base64
import re

MEMORY_CONVERSATION_TURN_BASE = "eidolon.memory.turn"
MEMORY_COMMAND_BASE = "eidolon.memory.cmd"
MEMORY_SYNC_BASE = "eidolon.memory.sync"

_MEMORY_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def validate_memory_space_id(memory_space_id: str) -> str:
    """Validate a memory realm id used as a subject suffix."""

    value = (memory_space_id or "").strip()
    if not _MEMORY_SPACE_ID_RE.fullmatch(value):
        raise ValueError(
            "memory_space_id must be a non-blank safe ASCII memory_realm_id; "
            f"got {memory_space_id!r}"
        )
    return value


def derive_memory_space_id(memory_realm_id: str) -> str:
    """Return the canonical memory-space id for a memory realm."""

    return validate_memory_space_id(memory_realm_id)


def memory_space_subject_token(memory_space_id: str) -> str:
    """Encode a memory_space_id as one NATS subject token.

    NATS treats ``.`` as a token separator, so the canonical
    ``tenant.owner.companion`` id must never be interpolated directly into a
    subject. Base64url without padding is reversible and collision-free for the
    validated input alphabet.
    """

    validated = validate_memory_space_id(memory_space_id)
    encoded = base64.urlsafe_b64encode(validated.encode("utf-8")).decode("ascii")
    return f"b64_{encoded.rstrip('=')}"


def memory_space_storage_name(memory_space_id: str) -> str:
    """Encode a memory_space_id as one filesystem path segment.

    The business id may contain characters that are technically allowed on
    POSIX but confusing in tools. For example, macOS Finder renders ``:`` as
    ``/``. Keep storage names derived, reversible, and collision-free instead
    of using raw realm ids as directory names.
    """

    return memory_space_subject_token(memory_space_id)


def conversation_turn_subject(memory_space_id: str) -> str:
    """Return the JetStream subject for a completed turn."""

    return f"{MEMORY_CONVERSATION_TURN_BASE}.{memory_space_subject_token(memory_space_id)}"


def conversation_turn_stream_pattern() -> str:
    """Wildcard subject pattern for conversation turn consumers."""

    return f"{MEMORY_CONVERSATION_TURN_BASE}.*"


def memory_command_subject(memory_space_id: str) -> str:
    """Return the JetStream subject for memory command writes."""

    return f"{MEMORY_COMMAND_BASE}.{memory_space_subject_token(memory_space_id)}"


def memory_command_stream_pattern() -> str:
    """Wildcard subject pattern for memory command consumers."""

    return f"{MEMORY_COMMAND_BASE}.*"


def memory_sync_subject(memory_space_id: str) -> str:
    """Return the JetStream subject for offline-device sync batches."""

    return f"{MEMORY_SYNC_BASE}.{memory_space_subject_token(memory_space_id)}"


def memory_sync_stream_pattern() -> str:
    """Wildcard subject pattern for offline-device sync batches."""

    return f"{MEMORY_SYNC_BASE}.*"


def all_memory_stream_patterns() -> list[str]:
    """Subjects the memory JetStream stream should bind."""

    return [
        conversation_turn_stream_pattern(),
        memory_command_stream_pattern(),
        memory_sync_stream_pattern(),
    ]
