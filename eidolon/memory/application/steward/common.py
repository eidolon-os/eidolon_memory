"""Shared steward helpers."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from eidolon.memory.domain.errors import MemoryBackendUnsupported
from eidolon.memory.support.logging import get_logger

if TYPE_CHECKING:
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.ports import MemoryBackend
    from eidolon.memory.domain.steward import PrivacyAction

log = get_logger(__name__)


def normalize_content(text: str) -> str:
    """Normalize content for stable hashing."""
    return re.sub(r"\s+", " ", text).strip()


def stable_fragment_id(
    *,
    user_id: str,
    source_turn_id: str,
    index: int,
    content: str,
) -> str:
    """Create a deterministic fragment id for retry-safe writes."""
    raw = f"{user_id}\n{source_turn_id}\n{index}\n{normalize_content(content)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_room_token(text: str, *, prefix: str = "topic") -> str:
    """Create a compact room key from user-facing text."""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "general"
    return f"{prefix}_{cleaned[:48]}"


def finalize_fragments(fragments: list[MemoryFragment], *, steward: str) -> list[MemoryFragment]:
    """Fill stable ids and metadata used by all steward implementations."""
    out: list[MemoryFragment] = []
    for index, frag in enumerate(fragments):
        if not frag.fragment_id:
            frag.fragment_id = stable_fragment_id(
                user_id=frag.user_id,
                source_turn_id=frag.source_turn_id,
                index=index,
                content=frag.content,
            )
        frag.metadata = {
            **frag.metadata,
            "fragment_id": frag.fragment_id,
            "source_turn_id": frag.source_turn_id,
            "schema_version": "1",
            "steward": steward,
            "importance": frag.importance,
            "confidence": frag.confidence,
            "memory_type": frag.memory_type,
            "privacy": frag.privacy,
        }
        out.append(frag)
    return out


async def apply_privacy_actions(
    backend: MemoryBackend,
    *,
    user_id: str,
    actions: list[PrivacyAction],
) -> None:
    """Best-effort archive/delete handling for privacy requests."""
    for action in actions:
        if action.action == "do_not_store":
            continue
        room = safe_room_token(action.target, prefix="privacy")
        try:
            await backend.delete(user_id, room)
        except MemoryBackendUnsupported as exc:
            log.warning(
                "privacy_action_backend_unsupported",
                action=action.action,
                target=action.target,
                error=str(exc),
            )
