"""Classify what the household Realm held before the layer existed (design §3.6).

The Owner's default Realm became the household layer. What it already held was
written when that Realm was one person's, so none of it has a layer, and the
household Realm recalls only what is household: until classified, all of it is
unclaimed — kept, listed, never recalled.

Each record is classified the way a new turn is: its user's own words (the
record's evidence quote) are read again by the production steward, and the
record is household only if the steward makes a household claim of those
words. Anything else is personal, which in the household Realm is unclaimed
still: the Owner can assign it to a Person or delete it. A dry run reports and
changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eidolon_memory_contracts import ConversationTurnPayload, MemoryActorContext

from eidolon.memory.application.privacy_filter import row_visible_to_listing
from eidolon.memory.domain.fragments import MemoryLayer


@dataclass(frozen=True, slots=True)
class LayerClassification:
    entry_id: str
    layer: MemoryLayer
    preview: str


def _covers(quote: str, claim_quote: str) -> bool:
    """Whether a claim quoting ``claim_quote`` is about the words ``quote``.

    The steward quotes its input verbatim, so a claim about these words quotes
    at least half of them.
    """

    return bool(claim_quote) and claim_quote in quote and len(claim_quote) >= 0.5 * len(quote)


async def classify_unlayered(
    records: list[Any],
    *,
    steward: Any,
    memory_space_id: str,
    owner_id: str | None,
) -> list[LayerClassification]:
    """The layer each unclassified record gets; nothing is written here."""

    results: list[LayerClassification] = []
    for record in records:
        metadata = record.metadata or {}
        if metadata.get("layer") or not row_visible_to_listing(record, include_private=False):
            continue
        quote = str(metadata.get("evidence_quote") or record.value or "").strip()
        if not quote:
            continue
        decision = await steward.decide(
            ConversationTurnPayload(
                turn_id=f"reclassify-{record.key}",
                context=MemoryActorContext(
                    memory_realm_id=memory_space_id,
                    owner_id=owner_id,
                    companion_id=str(metadata.get("companion_id") or "") or None,
                    session_id="household-reclassification",
                ),
                timestamp=str(metadata.get("occurred_at") or "1970-01-01T00:00:00Z"),
                user_text=quote,
                assistant_text="",
                metadata={"memory_ingest_policy": "semantic_steward"},
            )
        )
        household = any(
            fragment.layer == "household" and _covers(quote, fragment.evidence_quote)
            for fragment in decision.fragments
        )
        results.append(
            LayerClassification(
                entry_id=record.key,
                layer="household" if household else "personal",
                preview=str(record.value or "")[:80],
            )
        )
    return results


__all__ = ["LayerClassification", "classify_unlayered"]
