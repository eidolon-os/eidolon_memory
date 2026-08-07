"""Privacy-safe candidate resolution and verified drawer deletion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from eidolon.memory.application.kg_recall import plain_triple_sentence
from eidolon.memory.domain.ports import MemoryAdmin, MemoryPrivacyAdmin
from eidolon.memory.domain.wire import MemoryWireRecord

_COMMAND_RE = re.compile(
    r"(?:请|麻烦)?(?:帮我|把|给我)?(?:忘掉|删掉|删除|抹掉|不要记住|别记住|别记录|"
    r"不要再提|以后别再提|以后别提|别再提|别再说)"
)
_SUFFIX_RE = re.compile(r"(?:这条|这段|相关的|有关的)?(?:的)?(?:记忆|内容|信息|事情|事实)$")
_NON_SEMANTIC_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_GENERIC_TARGETS = frozenset({"", "刚才", "这件事", "那件事", "这个", "那个", "全部"})
DEFAULT_FORGET_PAGE_SIZE = 5_000


@dataclass(frozen=True, slots=True)
class ForgetCandidate:
    key: str
    text: str
    wing: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "drawer_id": self.key,
            "text": self.text,
            "wing": self.wing,
            "score": self.score,
        }


class ForgetResolutionLimitExceeded(RuntimeError):
    """Resolution cannot prove a complete candidate set within safety limits."""


def extract_privacy_target(text: str) -> str:
    """Remove command boilerplate while preserving the fact/topic itself."""
    clean = (text or "").strip().strip("，。！？,.!? ")
    clean = re.sub(r"^(?:请|麻烦)(?:帮我)?", "", clean).strip()
    clean = _COMMAND_RE.sub("", clean, count=1).strip("，。！？,.!? ")
    clean = _SUFFIX_RE.sub("", clean).strip("，。！？,.!? ")
    return clean


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
) -> list[ForgetCandidate]:
    """Return a bounded candidate set without silent static truncation.

    Pagination prevents a single multi-year Realm listing from materializing
    in memory. Hitting either safety cap raises instead of silently deleting a
    partial subset of the user's history. This is not a cross-page snapshot;
    exact-ID preview/confirm remains the strict path under concurrent writes.
    """
    clean_target = extract_privacy_target(target)
    normalized_target = _normalize(clean_target)
    if clean_target in _GENERIC_TARGETS or len(normalized_target) < 2:
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

    candidates.sort(key=lambda item: (-item.score, item.key))
    return candidates


async def source_turns_for_drawers(
    backend: MemoryAdmin,
    memory_space_id: str,
    drawer_ids: list[str],
) -> list[str]:
    """Which conversation turns produced these drawers.

    The bridge between the two stores in a forget. There is no fact-level
    identity shared by a drawer and a triple — the steward emits fragments and
    triples from one turn without claiming they correspond one to one — so the
    turn is the narrowest thing both sides can name, and the only one.

    Must be called *before* the drawers are mutated: the turn id lives in the
    drawer's metadata, so a deleted drawer takes the only pointer to its triples
    with it. That ordering is the reason this is a separate function rather than
    something the deletion helpers do on the way past.

    Missing drawers and drawers without a turn id are skipped rather than
    refused. A drawer predating the field, or one already gone, should not stop
    the rest of a confirmed privacy request from being honoured.
    """

    wanted = list(dict.fromkeys(k.strip() for k in drawer_ids if k.strip()))
    if not wanted:
        return []

    batch = getattr(backend, "get_many", None)
    if batch is not None:
        records = await batch(memory_space_id, wanted)
    else:
        # A backend that predates the plural. Correct, just a round trip each,
        # and the batch above exists because a hundred of those is the actual
        # cost of one privacy command.
        found = [await backend.get(memory_space_id, key) for key in wanted]
        records = [record for record in found if record is not None]

    turn_ids: list[str] = []
    seen: set[str] = set()
    for record in records:
        turn_id = str(record.metadata.get("source_turn_id") or "").strip()
        if turn_id and turn_id not in seen:
            seen.add(turn_id)
            turn_ids.append(turn_id)
    return turn_ids


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
    names = await kg.match_entities_for_query(phrase, cap=FORGET_ENTITY_CAP)
    if not names:
        return []
    triples = await kg.query_entity_combined(
        names,
        audiences=audiences,
        include_sensitive=True,
        limit_per_entity=FORGET_STATEMENTS_PER_ENTITY,
    )
    return [
        triple
        for triple in triples
        if _refers_to(phrase, plain_triple_sentence(triple))
    ]


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


async def forget_graph_for_drawers(
    backend: MemoryAdmin,
    kg: Any,
    memory_space_id: str,
    drawer_ids: list[str],
    *,
    hard: bool,
) -> int:
    """The graph half of a forget, for whichever path asked for one.

    There are two ways to be forgotten and they used to disagree. The confirmed
    MCP command and the steward acting on "忘掉…" mid-conversation both end at
    ``delete_exact_drawers`` / ``archive_exact_drawers``, and neither reached the
    graph; the second is the one people actually use, since it needs no tool call.
    Shared here so a third caller cannot arrive and quietly forget half again.

    **Call before mutating the drawers.** The turn id lives in drawer metadata, so
    a deleted drawer takes the only pointer to its triples with it — this reads
    them while they still exist. Graph first is also the safer order: its half is
    recoverable (an archive ends an interval, a hard forget is exported first),
    while ``delete_many`` is not.

    Returns statements affected; zero for a graph-less space, a turn that produced
    no triples, or drawers already gone.
    """

    if kg is None:
        return 0
    turn_ids = await source_turns_for_drawers(backend, memory_space_id, drawer_ids)
    if not turn_ids:
        return 0
    return await kg.forget_source_turns(turn_ids, hard=hard)


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
