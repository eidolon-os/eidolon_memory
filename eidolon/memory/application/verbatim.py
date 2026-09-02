"""The turn as the person said it, kept for a while.

Why this layer exists
---------------------

Until now the palace held only what the steward distilled. Measured on the
48-query battery: distillation answers 25-28 of 43, verbatim of the user's own
sentence answers 32, and **six queries are answerable only by one and six only
by the other**. Verbatim answers "what did I say" — pronouns, vague reference,
"上周聊了什么". Distillation answers "what does it mean" — "我答应了什么",
"我什么时候开心", phrasings the person never used. Neither substitutes.

Without this layer, whatever the steward misses at write time does not exist in
Memory afterwards: the decision ledger keeps a hash, not the text, so there is
nothing to re-read. That is why 41.7% was never a retrieval score. It is a
one-shot extraction ceiling, and no embedder or reranker moves it.

Why it is bounded
-----------------

Scale was measured rather than assumed, diluting the labelled corpus with 8379
real Chinese companion-style turns from CLongEval — same language, same genre,
so the dilution is as hard as production:

    40 drawers   32/43   74.4%   p95 15.1ms
   540 drawers   29/43   67.4%   p95 14.5ms
  2040 drawers   28/43   65.1%   p95 20.2ms
  8040 drawers   27/43   62.8%   p95 40.3ms

It degrades gracefully and sublinearly, and at 200x volume still scores inside
the band distillation reaches at 40. But the shape of that curve is the reason
retention is not optional: verbatim's value decays with volume while its cost
grows with it, so keeping everything forever spends the most exactly where it
helps the least. A window keeps the half that earns its space.

The retention shape is ``CommandStatusConfig``'s, deliberately: age cutoff,
then an overflow trim, triggered every N writes rather than by a background
job. The consolidator would have been the obvious host and is the wrong one —
it is an opt-in supervised subprocess and is **not running in production**, so
retention hung there would never have executed. That is the same failure as a
signal collected into a field nobody reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eidolon_memory_contracts import ConversationTurnPayload

from eidolon.memory.application.scope_policy import interaction_audience
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

#: What the drawer is, for anything that has to tell the kinds apart — recall
#: rendering, the retention sweep, an operator reading metadata.
VERBATIM_SOURCE = "turn-verbatim"

#: Raw conversation is an interaction, not a distilled profile fact. Filing it
#: under its own wing keeps it from competing with curated facts inside one
#: room and gives the sweep something cheap to select on.
VERBATIM_WING = "Wing_Interaction"
VERBATIM_ROOM = "conversation"


def verbatim_drawer(turn: ConversationTurnPayload) -> MemoryFragment | None:
    """One drawer holding the user's own sentence, or None if there is none.

    The assistant's half is deliberately excluded. It measured *worse* — 31 of
    43 against 32 with the user's text alone — because a paraphrase carries no
    new content and still competes for the five slots. It is also what
    ``test_steward_prompt_does_not_send_assistant_text`` already decided for
    extraction, for the separate reason that model prose must not become a
    second source of fact.
    """

    text = str(turn.user_text or "").strip()
    if not text:
        return None
    return MemoryFragment(
        memory_id=f"verbatim:{turn.turn_id}",
        memory_space_id=turn.context.memory_space_id,
        source_turn_id=turn.turn_id,
        session_id=turn.context.session_id,
        wing=VERBATIM_WING,
        room=VERBATIM_ROOM,
        content=text,
        # The person's own words are the evidence for anything derived from
        # them; ``evidence_quote`` on a fragment means the same thing one level
        # down.
        evidence_quote=text,
        memory_type="conversation",
        importance=2,
        confidence=1.0,
        occurred_at=turn.timestamp,
        audience=interaction_audience(turn.context),
        # The device that heard it, and only that device.
        #
        # Scope is content-dependent and the steward is what decides it — "这台
        # 设备在客厅，麦克风需要校准" is device-local, "我喜欢乌龙茶" is not — and
        # the steward has not run yet when this is written. The two ways of
        # guessing are not symmetric: guessing all_devices leaks a device-local
        # sentence to every device, and a multidevice E2E caught exactly that.
        # Guessing current_device only under-serves.
        #
        # It is also the more honest description. A raw utterance is this
        # interaction's own record of what was said to it; what generalises
        # across an owner's devices is the fact the steward distils from it,
        # and that projection carries its own scope decided on the content.
        scope="session",
        visibility="current_device",
        metadata={
            "source": VERBATIM_SOURCE,
            # Forgetting resolves by exact source event across every
            # projection, so this drawer is already covered by the privacy path
            # that exists — it needs no second delete route.
            "source_event_id": turn.turn_id,
        },
    )


def _filed_at(row: object) -> datetime | None:
    meta = getattr(row, "metadata", None) or {}
    for key in ("occurred_at", "filed_at", "indexed_at"):
        raw = str(meta.get(key) or "").strip()
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


async def prune_verbatim(
    backend: object,
    memory_space_id: str,
    *,
    retention_days: int,
    max_records: int,
    scan_limit: int = 20_000,
) -> int:
    """Drop verbatim drawers past the window, then trim the overflow.

    Returns how many were deleted. Never raises into the caller: losing a
    pruning pass costs disk, and failing a turn over it would trade the thing
    this layer exists to guarantee — that a turn is never lost — for tidiness.

    A drawer with no readable timestamp is kept. Deleting on a parse failure
    would make an unreadable date indistinguishable from an old one.
    """

    if retention_days < 1 or max_records < 1:
        return 0
    try:
        rows = await backend.get_all(memory_space_id, limit=scan_limit)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - pruning is best effort
        log.warning("verbatim_prune_scan_failed", error=str(exc))
        return 0

    mine = [
        row
        for row in rows
        if (getattr(row, "metadata", None) or {}).get("source") == VERBATIM_SOURCE
    ]
    # Clamped because this function promises not to raise into the caller and
    # the setting only bounds itself from below. A retention of a million days
    # overflowed the subtraction, and an OverflowError here would have taken
    # down the turn whose sentence this layer exists to keep.
    try:
        cutoff = datetime.now(UTC) - timedelta(days=min(retention_days, 36_500))
    except OverflowError:  # pragma: no cover - defensive
        return 0
    dated = [(row, _filed_at(row)) for row in mine]

    expired = [row for row, when in dated if when is not None and when < cutoff]
    kept = [(row, when) for row, when in dated if not (when is not None and when < cutoff)]

    overflow: list[object] = []
    if len(kept) > max_records:
        # Oldest first, undated last: an undated drawer is the one we know
        # least about, so it is the last thing to discard.
        undated_last = datetime.max.replace(tzinfo=UTC)
        kept.sort(key=lambda item: (item[1] is not None, item[1] or undated_last))
        ordered = [row for row, when in kept if when is not None]
        overflow = ordered[: max(0, len(kept) - max_records)]

    victims = expired + overflow
    keys = [str(getattr(row, "key", "") or "") for row in victims]
    keys = [key for key in keys if key]
    if not keys:
        return 0
    try:
        deleted = await backend.delete_many(memory_space_id, keys)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - pruning is best effort
        log.warning("verbatim_prune_delete_failed", error=str(exc), candidates=len(keys))
        return 0
    count = len(deleted) if isinstance(deleted, list) else len(keys)
    log.info(
        "verbatim_pruned",
        memory_space_id=memory_space_id,
        expired=len(expired),
        overflow=len(overflow),
        deleted=count,
    )
    return count
