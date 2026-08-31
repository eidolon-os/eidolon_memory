"""Deterministic translation from steward output to canonical memory intents."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from eidolon_memory_contracts import MemoryIntent, MemoryIntentType

from eidolon.memory.domain.steward import StewardDecision

_COMMITMENT_PREDICATES = {"promised", "committed_to", "planned_to"}
_PREFERENCE_PREDICATES = {"likes", "dislikes", "prefers"}
_EPISODE_PREDICATES = {"attended", "experienced", "achieved"}
_EPISODE_MEMORY_TYPES = {"interaction", "event", "emotion"}


def _intent_id(
    memory_space_id: str,
    source_event_id: str,
    source_kind: str,
    index: int,
    payload: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "memory_space_id": memory_space_id,
            "source_event_id": source_event_id,
            "source_kind": source_kind,
            "index": index,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"intent:{digest}"


def _fragment_intent_type(memory_type: str) -> MemoryIntentType:
    normalized = memory_type.strip().lower()
    if normalized == "preference":
        return "preference"
    if normalized in _EPISODE_MEMORY_TYPES:
        return "episode"
    # A goal is not automatically a promise.  Commitment requires an explicit
    # structured predicate or an explicit caller intent.
    return "fact"


def _triple_intent_type(predicate: str) -> MemoryIntentType:
    if predicate in _COMMITMENT_PREDICATES:
        return "commitment"
    if predicate in _PREFERENCE_PREDICATES:
        return "preference"
    if predicate in _EPISODE_PREDICATES:
        return "episode"
    return "fact"


def memory_intents_from_decision(
    decision: StewardDecision,
    *,
    memory_space_id: str,
    source_event_id: str,
) -> list[MemoryIntent]:
    """Translate every independently actionable steward output to an intent.

    The translation is deterministic and side-effect free.  It records what
    the extractor meant before Chroma/KG projection; it does not reconcile,
    deduplicate, supersede, or write memory state.
    """
    intents: list[MemoryIntent] = []

    for index, fragment in enumerate(decision.fragments):
        payload = fragment.model_dump(mode="json")
        attributes = {
            "source_kind": "fragment",
            "source_index": index,
            "wing": fragment.wing,
            "room": fragment.room,
            "memory_type": fragment.memory_type,
            "importance": fragment.importance,
            "tags": fragment.tags,
            "privacy": fragment.privacy,
            "scope": fragment.scope,
            "visibility": fragment.visibility,
            "evidence_quote": fragment.evidence_quote,
        }
        intents.append(
            MemoryIntent(
                intent_id=_intent_id(memory_space_id, source_event_id, "fragment", index, payload),
                memory_space_id=memory_space_id,
                source_event_id=source_event_id,
                authority="extracted_user",
                intent_type=_fragment_intent_type(fragment.memory_type),
                raw_claim=fragment.content,
                operation_hint="add",
                subject="$text",
                predicate="remembers_text",
                object=fragment.content,
                occurred_at=fragment.occurred_at,
                confidence=fragment.confidence,
                attributes=attributes,
            )
        )

    for index, triple in enumerate(decision.triples):
        payload = triple.model_dump(mode="json")
        intents.append(
            MemoryIntent(
                intent_id=_intent_id(memory_space_id, source_event_id, "triple", index, payload),
                memory_space_id=memory_space_id,
                source_event_id=source_event_id,
                authority="extracted_user",
                intent_type=_triple_intent_type(triple.predicate),
                raw_claim=f"{triple.subject} {triple.predicate} {triple.object}",
                operation_hint="add",
                subject=triple.subject,
                predicate=triple.predicate,
                object=triple.object,
                occurred_at=triple.valid_from,
                confidence=triple.confidence,
                attributes={
                    "source_kind": "triple",
                    "source_index": index,
                    "valid_to": triple.valid_to,
                    "evidence_quote": triple.evidence_quote,
                },
            )
        )

    for index, invalidation in enumerate(decision.invalidations):
        payload = invalidation.model_dump(mode="json")
        intents.append(
            MemoryIntent(
                intent_id=_intent_id(
                    memory_space_id, source_event_id, "invalidation", index, payload
                ),
                memory_space_id=memory_space_id,
                source_event_id=source_event_id,
                authority="extracted_user",
                intent_type="correction",
                raw_claim=(
                    f"invalidate {invalidation.subject} "
                    f"{invalidation.predicate} {invalidation.object}"
                ),
                operation_hint="invalidate",
                subject=invalidation.subject,
                predicate=invalidation.predicate,
                object=invalidation.object,
                occurred_at=invalidation.ended,
                confidence=1.0,
                attributes={
                    "source_kind": "invalidation",
                    "source_index": index,
                    "reason": invalidation.reason,
                    "evidence_quote": invalidation.evidence_quote,
                },
            )
        )

    for index, action in enumerate(decision.privacy_actions):
        payload = action.model_dump(mode="json")
        intents.append(
            MemoryIntent(
                intent_id=_intent_id(memory_space_id, source_event_id, "privacy", index, payload),
                memory_space_id=memory_space_id,
                source_event_id=source_event_id,
                authority="extracted_user",
                intent_type="forget",
                raw_claim=f"{action.action}: {action.target}",
                operation_hint="invalidate",
                target_id=action.target,
                confidence=1.0,
                attributes={
                    "source_kind": "privacy",
                    "source_index": index,
                    "privacy_action": action.action,
                    "reason": action.reason,
                    "evidence_quote": action.evidence_quote,
                },
            )
        )

    return intents
