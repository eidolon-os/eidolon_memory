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
    """Render one triple as a single readable Chinese line.

    This text goes into the ``[MEMORY]`` block and from there into the model's
    prompt, so a bad rendering is not cosmetic — it is a sentence the model reads
    as a fact about the user.
    """

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
        body = _predicate_template(t.predicate).format(s=subject, o=object_)
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


#: Predicate → sentence template. ``{s}`` is the subject, ``{o}`` the object.
#:
#: Every entry is a *full* template on purpose. The table used to mix two shapes:
#: relational predicates held a fragment with an ellipsis where the object went
#: ("是…的孩子"), while the rest held a bare verb ("喜欢"), and the renderer
#: concatenated subject + entry + object for both. So the eight relational ones
#: came out as "铁锤 是…的孩子 用户" and "用户 在…工作 某公司" — reaching the model
#: as broken sentences, in exactly the kinship and employment relations the
#: kinship_alias benchmark category tests. One shape makes that unrepresentable,
#: and a test asserts every entry carries both slots.
_PREDICATE_ZH = {
    "child_of": "{s} 是 {o} 的孩子",
    "parent_of": "{s} 是 {o} 的父母",
    "partner_of": "{s} 是 {o} 的伴侣",
    "sibling_of": "{s} 是 {o} 的兄弟姐妹",
    "friend_of": "{s} 和 {o} 是朋友",
    "colleague_of": "{s} 和 {o} 是同事",
    "works_at": "{s} 在 {o} 工作",
    "lives_in": "{s} 住在 {o}",
    "studies_at": "{s} 在 {o} 学习",
    "holds_role": "{s} 担任 {o}",
    "born_in": "{s} 出生于 {o}",
    "likes": "{s} 喜欢 {o}",
    "dislikes": "{s} 不喜欢 {o}",
    "prefers": "{s} 偏好 {o}",
    "does": "{s} 做 {o}",
    "practices": "{s} 在练习 {o}",
    "owns": "{s} 拥有 {o}",
    "uses": "{s} 在使用 {o}",
    "promised": "{s} 承诺 {o}",
    "committed_to": "{s} 承诺要 {o}",
    "planned_to": "{s} 计划 {o}",
    "has_state": "{s} 处于状态 {o}",
    "has_emotion": "{s} 感受到 {o}",
    "has_concern": "{s} 担心 {o}",
    "worried_about": "{s} 担心 {o}",
    "struggles_with": "{s} 在困扰于 {o}",
    "has_health_condition": "{s} 患有 {o}",
    "takes_medication": "{s} 在服用 {o}",
    "has_symptom": "{s} 有症状 {o}",
    "attended": "{s} 参加了 {o}",
    "experienced": "{s} 经历了 {o}",
    "achieved": "{s} 达成了 {o}",
}


def _predicate_template(p: str) -> str:
    """The sentence shape for ``p``, with ``{s}``/``{o}`` for subject and object.

    An unknown predicate falls back to bare juxtaposition, which is ugly but
    still parseable — better than dropping the fact.
    """

    return _PREDICATE_ZH.get(p, "{s} " + p + " {o}")


#: The graph stores the owner as the literal subject ``self``. Left untranslated
#: it reaches the model as "self 计划 去日本" — a schema token presented as part
#: of a fact about the user.
_SELF_LABEL = "用户"


def _entity_label(value: object) -> str:
    text = str(value or "")
    if text == "self":
        return _SELF_LABEL
    prefix, sep, label = text.partition(":")
    if sep and prefix.isascii() and prefix.replace("_", "").isalnum() and label:
        return label
    return text


def computed_kg_window_iso(window_days: int) -> str:
    """Earliest valid_from we'd accept for timeline-style queries."""
    return (datetime.now(UTC) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
