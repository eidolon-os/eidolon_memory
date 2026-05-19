"""KG side of the hybrid recall path (KG plan §5).

Two roles:

* **routing heuristic** — given a query string, pull the cached set of
  ``entities.name`` from KG and string-match. Cheap (<2 ms warm) so we can
  decide whether to fan out a KG read on the LiveKit hot path without an
  extra LLM call.
* **transcription** — turn ``KgTripleRecord`` rows into one-line natural-language
  context entries so the LLM consuming the recall context can treat KG facts
  identically to drawer fragments.

The lock on KG access is owned by ``LockedKnowledgeGraph``; this module is
stateless apart from the entity-name cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from eidolon.memory.domain.kg import KgTripleRecord


@dataclass
class _EntityCache:
    names: list[str]
    ts: float


_ENTITY_CACHE: dict[int, _EntityCache] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def cached_entity_names(kg, *, ttl_seconds: float = 60.0) -> list[str]:
    """Cache ``KG.list_entity_names`` for ``ttl_seconds`` per KG instance.

    Keyed on ``id(kg)`` rather than the SQLite path so concurrent agents in
    one Python interpreter (e.g., bench harness) don't share caches.
    """
    key = id(kg)
    now = time.monotonic()
    cached = _ENTITY_CACHE.get(key)
    if cached and now - cached.ts < ttl_seconds:
        return cached.names
    names = await kg.list_entity_names()
    _ENTITY_CACHE[key] = _EntityCache(names=names, ts=now)
    return names


def invalidate_entity_cache(kg) -> None:
    """Drop the cached list for one KG (call when a new entity is added)."""
    _ENTITY_CACHE.pop(id(kg), None)


def extract_entity_candidates(query: str, entity_names: list[str], *, cap: int) -> list[str]:
    """Return entities whose canonical name is a substring of ``query`` (cap N).

    Uses simple substring containment — fast, language-agnostic, no jieba
    required. Cap protects against pathological queries that mention every
    entity in the palace.
    """
    if not entity_names or not query:
        return []
    q = query.strip()
    if not q:
        return []
    # Order: longer names first to prefer "mother:张丽" over "mother".
    sorted_names = sorted(set(entity_names), key=lambda n: -len(n))
    hits: list[str] = []
    seen: set[str] = set()
    for name in sorted_names:
        if name in q and name not in seen:
            hits.append(name)
            seen.add(name)
            if len(hits) >= cap:
                break
    return hits


async def query_kg_for_recall(
    kg,
    *,
    entity_names: list[str],
    now_iso: str | None = None,
    window_days: int,
    max_triples_per_entity: int,
    include_sensitive: bool = False,
) -> list[KgTripleRecord]:
    """One SQL `IN (?,...)` over `entities` → triples; capped per entity."""
    if not entity_names:
        return []
    return await kg.query_entity_combined(
        entity_names,
        as_of=now_iso,
        include_sensitive=include_sensitive,
        limit_per_entity=max_triples_per_entity,
    )


def transcribe_triple(t: KgTripleRecord) -> str:
    """Render one triple as a single readable Chinese line (KG plan §5.5)."""
    valid_from = (t.valid_from or "").strip()
    valid_to = (t.valid_to or "").strip()
    body = f"{t.subject} {_predicate_zh(t.predicate)} {t.object}"
    qualifier: str
    if t.predicate == "promised" and valid_to:
        qualifier = f"（截至 {valid_to}）"
    elif valid_to:
        qualifier = f"（{valid_from or '？'} → {valid_to}，已结束）"
    elif valid_from:
        qualifier = f"（自 {valid_from}）"
    else:
        qualifier = ""
    return f"[KG] {body}{qualifier}"


def transcribe_triples(triples: list[KgTripleRecord]) -> str:
    if not triples:
        return ""
    lines = [transcribe_triple(t) for t in triples]
    return "知识图谱事实：\n" + "\n".join(f"- {x}" for x in lines)


_PREDICATE_ZH = {
    "child_of": "是…的孩子",
    "parent_of": "是…的父母",
    "partner_of": "是…的伴侣",
    "sibling_of": "是…的兄弟姐妹",
    "friend_of": "和…是朋友",
    "colleague_of": "和…是同事",
    "works_at": "在…工作",
    "lives_in": "住在",
    "studies_at": "在…学习",
    "holds_role": "担任",
    "born_in": "出生于",
    "likes": "喜欢",
    "dislikes": "不喜欢",
    "prefers": "偏好",
    "does": "做",
    "practices": "在练习",
    "owns": "拥有",
    "uses": "在使用",
    "promised": "承诺",
    "committed_to": "承诺要",
    "planned_to": "计划",
    "has_state": "处于状态",
    "has_emotion": "感受到",
    "has_concern": "担心",
    "worried_about": "担心",
    "struggles_with": "在困扰于",
    "has_health_condition": "患有",
    "takes_medication": "在服用",
    "has_symptom": "有症状",
    "attended": "参加了",
    "experienced": "经历了",
    "achieved": "达成了",
}


def _predicate_zh(p: str) -> str:
    return _PREDICATE_ZH.get(p, p)


def computed_kg_window_iso(window_days: int) -> str:
    """Earliest valid_from we'd accept for timeline-style queries."""
    return (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
