"""KG side of the hybrid recall path (KG plan §5).

One role:

* **transcription** — turn ``KgTripleRecord`` rows into one-line natural-language
  context entries so the LLM consuming the recall context can treat KG facts
  identically to drawer fragments.

Entity routing(自然语言 query → canonical entity names)lives on the KG
facade as :meth:`KnowledgeGraphPort.match_entities_for_query`, because
the naming convention(``pet:`` / ``place:`` / ``mother:`` 前缀)is KG's
internal knowledge — the recall router shouldn't need to know about it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eidolon.memory.domain.kg import KgTripleRecord


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def query_kg_for_recall(
    kg,
    *,
    audiences: tuple[str, ...],
    entity_names: list[str],
    subject_names: list[str] | None = None,
    now_iso: str | None = None,
    window_days: int,
    max_triples_per_entity: int,
    include_sensitive: bool = False,
) -> list[KgTripleRecord]:
    """Statements relevant to a turn, bounded per entity and scoped to audience.

    ``audiences`` is required rather than defaulted. A default would be either
    too narrow (the owner layer only, quietly losing what this companion was
    told) or too wide (everything, showing one companion what another was told) —
    and the wide mistake is invisible until there is a second companion.
    """

    if subject_names:
        return await kg.query_subjects(
            subject_names,
            audiences=audiences,
            as_of=now_iso,
            include_sensitive=include_sensitive,
            limit_per_subject=max_triples_per_entity,
        )
    if not entity_names:
        return []
    return await kg.query_entity_combined(
        entity_names,
        audiences=audiences,
        as_of=now_iso,
        include_sensitive=include_sensitive,
        limit_per_entity=max_triples_per_entity,
    )


def transcribe_triple(t: KgTripleRecord) -> str:
    """Render one triple as a single readable Chinese line (KG plan §5.5)."""
    valid_from = (t.valid_from or "").strip()
    valid_to = (t.valid_to or "").strip()
    subject = _entity_label(t.subject)
    object_ = _entity_label(t.object)
    if t.predicate == "holds_role":
        if str(t.subject).startswith("pet:"):
            body = f"{subject} 的品种/身份是 {object_}"
        else:
            body = f"{subject} 的角色/身份是 {object_}"
    else:
        body = f"{subject} {_predicate_zh(t.predicate)} {object_}"
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


def _entity_label(value: object) -> str:
    text = str(value or "")
    prefix, sep, label = text.partition(":")
    if sep and prefix.isascii() and prefix.replace("_", "").isalnum() and label:
        return label
    return text


def computed_kg_window_iso(window_days: int) -> str:
    """Earliest valid_from we'd accept for timeline-style queries."""
    return (datetime.now(UTC) - timedelta(days=window_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
