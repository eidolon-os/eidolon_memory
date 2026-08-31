"""Privacy-safe candidate resolution and verified drawer deletion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from eidolon.memory.application.kg_recall import plain_triple_sentence
from eidolon.memory.domain.ports import (
    CommitmentReader,
    CommitmentWriter,
    MemoryAdmin,
    MemoryPrivacyAdmin,
)
from eidolon.memory.domain.wire import MemoryWireRecord

_NON_SEMANTIC_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
DEFAULT_FORGET_PAGE_SIZE = 5_000


@dataclass(frozen=True, slots=True)
class ForgetCandidate:
    key: str
    text: str
    wing: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        result = {
            "id": self.key,
            "text": self.text,
            "wing": self.wing,
            "score": self.score,
        }
        if self.key.startswith("drawer_"):
            result["drawer_id"] = self.key
            result["kind"] = "assertion"
        elif self.key.startswith("commitment:"):
            result["commitment_id"] = self.key
            result["kind"] = "commitment"
        return result


class ForgetResolutionLimitExceeded(RuntimeError):
    """Resolution cannot prove a complete candidate set within safety limits."""


def normalize_privacy_target(text: str) -> str:
    """Normalize an already-resolved semantic target without language rules."""

    return (text or "").strip().strip("，。！？,.!? ")


def _normalize(text: Any) -> str:
    return _NON_SEMANTIC_RE.sub("", str(text or "").lower())


def _belongs_to_space(record: MemoryWireRecord, memory_space_id: str) -> bool:
    recorded_space = str(record.metadata.get("memory_space_id") or record.memory_space_id)
    return recorded_space == memory_space_id


async def find_forget_candidates(
    backend: MemoryAdmin,
    memory_space_id: str,
    target: str,
    *,
    max_scan: int = 50_000,
    max_candidates: int = 20,
    page_size: int = DEFAULT_FORGET_PAGE_SIZE,
    commitments: CommitmentReader | None = None,
) -> list[ForgetCandidate]:
    """Return a bounded candidate set without silent static truncation.

    Pagination prevents a single multi-year Realm listing from materializing
    in memory. Hitting either safety cap raises instead of silently deleting a
    partial subset of the user's history. This is not a cross-page snapshot;
    exact-ID preview/confirm remains the strict path under concurrent writes.
    """
    clean_target = normalize_privacy_target(target)
    normalized_target = _normalize(clean_target)
    if len(normalized_target) < 2:
        return []

    # Stable IDs are already unambiguous; avoid an O(n) palace listing for
    # exact-delete flows and large realms.
    if clean_target.startswith("drawer_"):
        record = await backend.get(memory_space_id, clean_target)
        if record is None or not _belongs_to_space(record, memory_space_id):
            return []
        return [
            ForgetCandidate(
                key=record.key,
                text=str(record.value or ""),
                wing=str(record.metadata.get("wing") or ""),
                score=1.0,
            )
        ]

    if max_scan < 1 or max_candidates < 1 or page_size < 1:
        raise ValueError("forget resolution limits must be positive")

    candidates: list[ForgetCandidate] = []
    candidate_keys: set[str] = set()
    offset = 0
    chunk = min(page_size, max_scan)
    while offset < max_scan:
        request_size = min(chunk, max_scan - offset)
        rows = await backend.get_all(
            memory_space_id,
            limit=request_size,
            offset=offset,
        )
        if not rows:
            break
        offset += len(rows)
        for record in rows:
            if not _belongs_to_space(record, memory_space_id):
                continue
            # A commitment drawer is a projection of the commitment ledger, not
            # a canonical fact. Returning it here would mint a token that later
            # sends it through the assertion ledger and correctly fails as a
            # non-canonical drawer. Resolve the aggregate below instead, once.
            if str(record.metadata.get("commitment_id") or "").strip():
                continue
            normalized_key = _normalize(record.key)
            normalized_text = _normalize(record.value)
            memory_id = _normalize(record.metadata.get("memory_id"))
            if normalized_target in {normalized_key, memory_id}:
                score = 1.0
            elif normalized_target == normalized_text:
                score = 1.0
            elif normalized_target in normalized_text:
                score = 0.9
            elif len(normalized_text) >= 4 and normalized_text in normalized_target:
                score = 0.85
            else:
                continue
            if record.key in candidate_keys:
                continue
            candidate_keys.add(record.key)
            candidates.append(
                ForgetCandidate(
                    key=record.key,
                    text=str(record.value or ""),
                    wing=str(record.metadata.get("wing") or ""),
                    score=score,
                )
            )
            if len(candidates) > max_candidates:
                raise ForgetResolutionLimitExceeded(
                    f"privacy target matches more than {max_candidates} drawers"
                )
        if len(rows) < request_size:
            break

    if offset >= max_scan:
        overflow = await backend.get_all(memory_space_id, limit=1, offset=offset)
        if overflow:
            raise ForgetResolutionLimitExceeded(
                f"privacy candidate scan exceeds {max_scan} drawers; use exact drawer IDs"
            )

    if commitments is not None:
        commitment_offset = 0
        while commitment_offset < max_scan:
            request_size = min(chunk, max_scan - commitment_offset)
            records = await commitments.list_for_privacy(
                memory_space_id,
                limit=request_size,
                offset=commitment_offset,
            )
            if not records:
                break
            commitment_offset += len(records)
            for record in records:
                normalized_id = _normalize(record.commitment_id)
                normalized_action = _normalize(record.action)
                if normalized_target == normalized_id:
                    score = 1.0
                elif normalized_target == normalized_action:
                    score = 1.0
                elif normalized_target in normalized_action:
                    score = 0.9
                elif len(normalized_action) >= 4 and normalized_action in normalized_target:
                    score = 0.85
                else:
                    continue
                if record.commitment_id in candidate_keys:
                    continue
                candidate_keys.add(record.commitment_id)
                candidates.append(
                    ForgetCandidate(
                        key=record.commitment_id,
                        text=record.action,
                        wing="Wing_Future",
                        score=score,
                    )
                )
                if len(candidates) > max_candidates:
                    raise ForgetResolutionLimitExceeded(
                        f"privacy target matches more than {max_candidates} memories"
                    )
            if len(records) < request_size:
                break
        if commitment_offset >= max_scan:
            overflow = await commitments.list_for_privacy(
                memory_space_id, limit=1, offset=commitment_offset
            )
            if overflow:
                raise ForgetResolutionLimitExceeded(
                    f"privacy candidate scan exceeds {max_scan} commitments; use an exact ID"
                )

    candidates.sort(key=lambda item: (-item.score, item.key))
    return candidates


async def assertion_ids_for_drawers(
    backend: MemoryAdmin,
    memory_space_id: str,
    drawer_ids: list[str],
) -> list[str]:
    """Stable ledger identities carried by exact drawer projections."""

    wanted = list(dict.fromkeys(key.strip() for key in drawer_ids if key.strip()))
    if not wanted:
        return []
    batch = getattr(backend, "get_many", None)
    records = (
        await batch(memory_space_id, wanted)
        if batch is not None
        else [record for record in await _get_records(backend, memory_space_id, wanted)]
    )
    assertion_ids: list[str] = []
    seen: set[str] = set()
    for record in records:
        assertion_id = str(record.metadata.get("assertion_id") or "").strip()
        if assertion_id and assertion_id not in seen:
            seen.add(assertion_id)
            assertion_ids.append(assertion_id)
    return assertion_ids


async def _get_records(
    backend: MemoryAdmin, memory_space_id: str, drawer_ids: list[str]
) -> list[MemoryWireRecord]:
    found = [await backend.get(memory_space_id, key) for key in drawer_ids]
    return [record for record in found if record is not None]


#: How many entities a forget target is resolved to before its statements are read.
#:
#: Higher than recall's three: a recall is shaping a prompt and can afford to miss
#: an entity, while a forget that misses one leaves a memory the person asked to
#: be rid of. Still bounded, because the target is a phrase and an unbounded read
#: on a privacy path is how a graph-sized query gets onto a four-core board.
FORGET_ENTITY_CAP = 8

#: How many statements per entity are considered. Above the recall budget for the
#: same reason.
FORGET_STATEMENTS_PER_ENTITY = 50


async def find_forget_statements(
    kg: Any,
    target: str,
    *,
    audiences: tuple[str, ...],
) -> list[Any]:
    """Statements the target refers to, for facts no drawer holds.

    **The hole this closes.** Fragments and triples pass independent gates —
    ``min_importance_to_write`` of 3 against ``min_confidence_to_write`` of 0.6 —
    so a sentence that is ordinary but reliable ("用户喜欢绿茶", importance 2,
    confidence 0.9) becomes a triple and no drawer. Asking to forget it scanned
    drawers, found nothing, recorded ``unmatched_targets``, and did nothing at
    all, while the statement went on being rendered into every later prompt.

    Resolved through the same machinery a question goes through: entities named
    in the phrase, then their statements, then the *same containment rule* the
    drawer path scores with — applied to the bare sentence, not the rendered
    line, because "（推测）" and "（根据 2026-03 的对话）" would stop any literal
    phrase from matching.

    Sensitive statements are included. A person asking to forget a health fact is
    the one case where the read policy that normally hides them is precisely
    backwards: refusing to see it would mean refusing to forget it.
    """

    if kg is None:
        return []
    phrase = (target or "").strip()
    if not phrase:
        return []
    names = await kg.match_entities_for_query(
        phrase,
        audiences=audiences,
        cap=FORGET_ENTITY_CAP,
    )
    if not names:
        return []
    triples = await kg.query_entity_combined(
        names,
        audiences=audiences,
        include_sensitive=True,
        limit_per_entity=FORGET_STATEMENTS_PER_ENTITY,
    )
    return [triple for triple in triples if _refers_to(phrase, plain_triple_sentence(triple))]


def _refers_to(target: str, sentence: str) -> bool:
    """The drawer path's scoring rule, as a yes or no.

    ``find_forget_candidates`` grades containment 1.0 / 0.9 / 0.85 and keeps
    anything that scores; the grades only order the list. Reusing the same rule
    rather than inventing a second one is the point — one way to decide what a
    forget refers to, whichever store holds it.
    """

    left = _normalize(target)
    right = _normalize(sentence)
    if not left or not right:
        return False
    return left == right or left in right or (len(right) >= 4 and right in left)


async def forget_exact_projections(
    backend: MemoryAdmin,
    kg: Any,
    canonical_facts: Any,
    memory_space_id: str,
    drawer_ids: list[str],
    *,
    hard: bool,
) -> tuple[list[str], int]:
    """Ledger-first deletion of one exact projection set.

    The canonical tombstone is written first and remains pending until both KG
    and drawer mutations verify. A redelivery resumes the same outbox entry; a
    replay of the original evidence cannot reactivate a forgotten assertion.
    """

    assertion_ids = await assertion_ids_for_drawers(backend, memory_space_id, drawer_ids)
    if not assertion_ids:
        raise RuntimeError("privacy mutation refused a non-canonical drawer")
    if canonical_facts is None:
        raise RuntimeError("canonical drawer forget requires its fact ledger")
    targets = {"drawer", "kg"} if kg is not None else {"drawer"}
    ledger_assertions = await canonical_facts.begin_forget(
        memory_space_id,
        assertion_ids,
        hard=hard,
        reason="user privacy request",
        targets=targets,
    )
    if set(ledger_assertions) != set(assertion_ids):
        raise RuntimeError("drawer projection points to a missing canonical assertion")

    statements = 0
    if kg is not None:
        statements = await kg.forget_assertions(ledger_assertions, hard=hard)
        await canonical_facts.mark_forget_projected(
            memory_space_id, ledger_assertions, targets={"kg"}
        )

    changed = (
        await delete_exact_drawers(backend, memory_space_id, drawer_ids)
        if hard
        else await archive_exact_drawers(backend, memory_space_id, drawer_ids)
    )
    if ledger_assertions:
        await canonical_facts.mark_forget_projected(
            memory_space_id, ledger_assertions, targets={"drawer"}
        )
    return changed, statements


async def forget_commitment_projections(
    backend: MemoryAdmin,
    kg: Any,
    commitments: CommitmentWriter,
    memory_space_id: str,
    commitment_ids: list[str],
    *,
    hard: bool,
) -> tuple[list[str], int]:
    """Tombstone commitment content before removing either projection.

    The retained row contains only the stable commitment id, revision count and
    projection states. It is enough to resume an interrupted delete and reject
    replay, without preserving the promise text the person asked us to remove.
    """

    wanted = list(dict.fromkeys(value.strip() for value in commitment_ids if value.strip()))
    if not wanted:
        return [], 0
    plans = await commitments.begin_forget(memory_space_id, wanted, hard=hard)
    if {plan.commitment_id for plan in plans} != set(wanted):
        raise RuntimeError("commitment privacy ledger did not accept every target")

    statements = 0
    if kg is not None:
        statements = await kg.forget_source_turns(wanted, hard=hard)
    await commitments.mark_forget_projected(memory_space_id, wanted, targets={"kg"})

    drawer_ids: list[str] = []
    for plan in plans:
        for revision in range(1, plan.revision_count + 1):
            record = await backend.get_by_source_turn_id(
                memory_space_id,
                f"{plan.commitment_id}:revision:{revision}",
            )
            if record is not None:
                drawer_ids.append(record.key)
    changed: list[str] = []
    if drawer_ids:
        changed = (
            await backend.delete_many(memory_space_id, drawer_ids)
            if hard
            else await backend.archive_many(memory_space_id, drawer_ids)
        )
    await commitments.mark_forget_projected(memory_space_id, wanted, targets={"drawer"})
    await commitments.finalize_forget(memory_space_id, wanted)
    return changed, statements


async def forget_graph_assertions(
    kg: Any,
    canonical_facts: Any,
    memory_space_id: str,
    assertion_ids: list[str],
) -> int:
    """Archive graph-only projections through their canonical ledger identity."""

    wanted = list(dict.fromkeys(value.strip() for value in assertion_ids if value.strip()))
    if not wanted:
        raise RuntimeError("privacy mutation refused a non-canonical graph projection")
    if canonical_facts is None:
        raise RuntimeError("canonical graph forget requires its fact ledger")
    ledger_assertions = await canonical_facts.begin_forget(
        memory_space_id,
        wanted,
        hard=False,
        reason="user privacy request",
        targets={"kg"},
    )
    if set(ledger_assertions) != set(wanted):
        raise RuntimeError("graph projection points to a missing canonical assertion")
    changed = await kg.forget_assertions(ledger_assertions, hard=False)
    await canonical_facts.mark_forget_projected(memory_space_id, ledger_assertions, targets={"kg"})
    return changed


async def delete_exact_drawers(
    backend: MemoryPrivacyAdmin,
    memory_space_id: str,
    drawer_ids: list[str],
) -> list[str]:
    """Delegate one confirmed ID batch to the privacy mutation port."""
    unique_ids = list(dict.fromkeys(key.strip() for key in drawer_ids if key.strip()))
    if not unique_ids:
        raise ValueError("at least one drawer_id is required")

    for key in unique_ids:
        if not key.startswith("drawer_"):
            raise ValueError(f"invalid MemPalace drawer_id: {key}")
    return await backend.delete_many(memory_space_id, unique_ids)


async def archive_exact_drawers(
    backend: MemoryPrivacyAdmin,
    memory_space_id: str,
    drawer_ids: list[str],
) -> list[str]:
    """Mark one confirmed ID batch do-not-recall and verify the policy."""
    unique_ids = list(dict.fromkeys(key.strip() for key in drawer_ids if key.strip()))
    if not unique_ids:
        raise ValueError("at least one drawer_id is required")
    for key in unique_ids:
        if not key.startswith("drawer_"):
            raise ValueError(f"invalid MemPalace drawer_id: {key}")
    return await backend.archive_many(memory_space_id, unique_ids)
