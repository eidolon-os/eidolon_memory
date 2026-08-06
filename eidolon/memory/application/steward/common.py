"""Shared steward helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eidolon.memory.application.forget import (
    archive_exact_drawers,
    delete_exact_drawers,
    find_forget_candidates,
    forget_graph_for_drawers,
)
from eidolon.memory.domain.errors import MemoryBackendUnsupported
from eidolon.memory.support.logging import get_logger

if TYPE_CHECKING:
    from eidolon.memory.domain.fragments import MemoryFragment
    from eidolon.memory.domain.ports import MemoryBackend
    from eidolon.memory.domain.steward import PrivacyAction

log = get_logger(__name__)


@dataclass(slots=True)
class PrivacyActionResult:
    deleted_keys: list[str] = field(default_factory=list)
    archived_keys: list[str] = field(default_factory=list)
    unmatched_targets: list[str] = field(default_factory=list)
    confirmation_required: dict[str, list[dict[str, object]]] = field(
        default_factory=dict
    )
    #: Graph statements forgotten alongside the drawers. Separate from the key
    #: lists because they count different things, and because a batch that
    #: touched drawers and no statements on a graph-enabled space is the shape
    #: the old bug had.
    statements_forgotten: int = 0


def normalize_content(text: str) -> str:
    """Normalize content for stable hashing."""
    return re.sub(r"\s+", " ", text).strip()


def stable_fragment_id(
    *,
    memory_space_id: str,
    source_turn_id: str,
    index: int,
    content: str,
) -> str:
    """Create a deterministic fragment id for retry-safe writes."""
    raw = f"{memory_space_id}\n{source_turn_id}\n{index}\n{normalize_content(content)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_room_token(text: str, *, prefix: str = "topic") -> str:
    """Create a compact room key from user-facing text."""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "general"
    return f"{prefix}_{cleaned[:48]}"


def stamp_fragment_identity(
    fragment: MemoryFragment,
    *,
    context: object,
    source_turn_id: str | None = None,
) -> MemoryFragment:
    """Stamp runtime identity from the authoritative turn context."""

    memory_realm_id = str(getattr(context, "memory_realm_id", "") or "").strip()
    memory_space_id = str(getattr(context, "memory_space_id", "") or "").strip()
    if not memory_space_id:
        memory_space_id = memory_realm_id
    owner_id = getattr(context, "owner_id", None)
    companion_id = getattr(context, "companion_id", None)
    device_id = getattr(context, "device_id", None)
    session_id = getattr(context, "session_id", None)
    updates = {
        "memory_space_id": memory_space_id,
        "memory_realm_id": memory_realm_id or memory_space_id,
        "owner_id": owner_id,
        "companion_id": companion_id,
        "source_device_id": device_id,
        "source_instance_id": companion_id,
        "source_turn_id": source_turn_id or fragment.source_turn_id,
        "session_id": session_id,
    }
    if fragment.scope == "device" and not fragment.target_device_id:
        updates["target_device_id"] = device_id
    return fragment.model_copy(update=updates)


def finalize_fragments(
    fragments: list[MemoryFragment],
    *,
    steward: str,
    context: object | None = None,
    source_turn_id: str | None = None,
) -> list[MemoryFragment]:
    """Fill stable ids and metadata used by all steward implementations."""
    out: list[MemoryFragment] = []
    for index, frag in enumerate(fragments):
        if context is not None:
            frag = stamp_fragment_identity(
                frag,
                context=context,
                source_turn_id=source_turn_id,
            )
        if not frag.memory_id:
            frag.memory_id = stable_fragment_id(
                memory_space_id=frag.memory_space_id,
                source_turn_id=frag.source_turn_id,
                index=index,
                content=frag.content,
            )
        frag.metadata = {
            **frag.metadata,
            "memory_id": frag.memory_id,
            "memory_space_id": frag.memory_space_id,
            "memory_realm_id": frag.memory_realm_id or frag.memory_space_id,
            "owner_id": frag.owner_id or "",
            "companion_id": frag.companion_id or "",
            "scope": frag.scope,
            "visibility": frag.visibility,
            "source_device_id": frag.source_device_id or "",
            "target_device_id": frag.target_device_id or "",
            "source_instance_id": frag.source_instance_id or "",
            "source_companion_id": frag.companion_id or frag.source_instance_id or "",
            "source_turn_id": frag.source_turn_id,
            "session_id": frag.session_id or "",
            "schema_version": "2",
            "steward": steward,
            "importance": frag.importance,
            "confidence": frag.confidence,
            "memory_type": frag.memory_type,
            "privacy": frag.privacy,
            "extensions": frag.extensions,
        }
        out.append(frag)
    return out


async def apply_privacy_actions(
    backend: MemoryBackend,
    *,
    memory_space_id: str,
    actions: list[PrivacyAction],
    kg: Any = None,
) -> PrivacyActionResult:
    """Resolve targets, then run a serialized and verified privacy batch.

    Resolution is read-only and deliberately separate from mutation. The
    backend therefore guarantees the selected IDs, not a serializable
    natural-language predicate spanning both calls.

    ``kg`` is optional because the graph is, and defaults to ``None`` for the same
    reason every other graph call site does. That default is also a hazard worth
    naming: this ran without a graph argument at all until 2026-08-06, so a
    "忘掉…" said in conversation removed the drawer and left the triple to be
    rendered into the next prompt. **This is the path people actually take** — it
    needs no tool call and no confirmation round trip — so it was the more common
    half of the same defect, fixed later than the rarer half.

    Passing ``kg=None`` from a caller that has a graph therefore silently restores
    the bug. All three call sites pass it; a fourth must too.
    """
    result = PrivacyActionResult()
    for action in actions:
        if action.action == "do_not_store":
            continue
        try:
            candidates = await find_forget_candidates(
                backend,
                memory_space_id,
                action.target,
            )
            if not candidates:
                result.unmatched_targets.append(action.target)
                log.warning(
                    "privacy_action_no_candidate",
                    action=action.action,
                    target=action.target,
                )
                continue
            keys = [candidate.key for candidate in candidates]
            if action.action == "archive_topic":
                result.statements_forgotten += await forget_graph_for_drawers(
                    backend, kg, memory_space_id, keys, hard=False
                )
                archived = await archive_exact_drawers(
                    backend,
                    memory_space_id,
                    keys,
                )
                result.archived_keys.extend(archived)
            else:
                if len(candidates) > 1:
                    result.confirmation_required[action.target] = [
                        candidate.to_dict() for candidate in candidates
                    ]
                    log.warning(
                        "privacy_delete_confirmation_required",
                        target=action.target,
                        candidate_count=len(candidates),
                    )
                    continue
                # After the ambiguity check, not before: an unconfirmed delete
                # must not forget triples it has already declined to forget
                # drawers for.
                result.statements_forgotten += await forget_graph_for_drawers(
                    backend, kg, memory_space_id, keys, hard=True
                )
                deleted = await delete_exact_drawers(
                    backend,
                    memory_space_id,
                    keys,
                )
                result.deleted_keys.extend(deleted)
        except MemoryBackendUnsupported as exc:
            log.warning(
                "privacy_action_backend_unsupported",
                action=action.action,
                target=action.target,
                error=str(exc),
            )
            result.unmatched_targets.append(action.target)
    return result
