"""Product-owned predicate semantics for deterministic fact reconciliation.

The SDK whitelist controls which predicates may cross the wire.  This registry
adds the narrower business semantics that belong to the Memory domain.  An
unknown predicate therefore fails closed instead of inheriting an accidental
cardinality or update policy from an LLM prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from eidolon_memory_contracts import KG_PREDICATE_VALUES, MemoryIntentType


class PredicateCardinality(StrEnum):
    SINGLE = "single"
    MULTI = "multi"


class PredicateTemporality(StrEnum):
    CURRENT_STATE = "current_state"
    DURABLE = "durable"
    EVENT = "event"


class PredicateUpdatePolicy(StrEnum):
    """How a different object in the same subject/predicate slot is handled."""

    SUPERSEDE_EXPLICIT = "supersede_explicit"
    REQUIRE_CORRECTION = "require_correction"
    EXACT_ONLY = "exact_only"


@dataclass(frozen=True, slots=True)
class PredicateDefinition:
    predicate: str
    cardinality: PredicateCardinality
    temporality: PredicateTemporality
    update_policy: PredicateUpdatePolicy = PredicateUpdatePolicy.EXACT_ONLY
    sensitive: bool = False
    intent_type: MemoryIntentType = "fact"
    projection_wing: str | None = None
    projection_memory_type: str | None = None


def _definition(
    predicate: str,
    *,
    cardinality: PredicateCardinality = PredicateCardinality.MULTI,
    temporality: PredicateTemporality = PredicateTemporality.DURABLE,
    update_policy: PredicateUpdatePolicy = PredicateUpdatePolicy.EXACT_ONLY,
    sensitive: bool = False,
    intent_type: MemoryIntentType = "fact",
    projection_wing: str | None = None,
    projection_memory_type: str | None = None,
) -> PredicateDefinition:
    return PredicateDefinition(
        predicate=predicate,
        cardinality=cardinality,
        temporality=temporality,
        update_policy=update_policy,
        sensitive=sensitive,
        intent_type=intent_type,
        projection_wing=projection_wing,
        projection_memory_type=projection_memory_type,
    )


# Conservative by design.  Only ``lives_in`` currently has product semantics
# strong enough to replace another current value automatically, and even then
# only for an explicit ``update`` command.  Employment, roles, preferences,
# relationships, health facts and commitments may all legitimately be plural.
_PREDICATES: dict[str, PredicateDefinition] = {}


def _register_projection(
    predicates: tuple[str, ...],
    *,
    wing: str,
    memory_type: str,
    intent_type: MemoryIntentType = "fact",
) -> None:
    """Register product ontology, never infer it from a claim's wording."""

    for predicate in predicates:
        _PREDICATES[predicate] = _definition(
            predicate,
            intent_type=intent_type,
            projection_wing=wing,
            projection_memory_type=memory_type,
        )


_register_projection(
    (
        "child_of",
        "parent_of",
        "partner_of",
        "sibling_of",
        "friend_of",
    ),
    wing="Wing_Relationship",
    memory_type="relationship",
)
_register_projection(
    ("colleague_of", "works_at", "studies_at", "holds_role"),
    wing="Wing_Work",
    memory_type="work",
)
_register_projection(
    ("lives_in", "born_in"),
    wing="Wing_Profile",
    memory_type="profile",
)
_register_projection(
    ("likes", "dislikes", "prefers"),
    wing="Wing_Life",
    memory_type="preference",
    intent_type="preference",
)
_register_projection(
    ("does", "practices", "owns", "uses"),
    wing="Wing_Life",
    memory_type="life",
)
_register_projection(
    ("promised", "committed_to", "planned_to"),
    wing="Wing_Future",
    memory_type="commitment",
    intent_type="commitment",
)
_register_projection(
    (
        "has_state",
        "has_emotion",
        "has_concern",
        "worried_about",
        "struggles_with",
    ),
    wing="Wing_Emotion",
    memory_type="emotion",
)
_register_projection(
    ("has_health_condition", "takes_medication", "has_symptom"),
    wing="Wing_Health",
    memory_type="health",
)
_register_projection(
    ("attended", "experienced", "achieved"),
    wing="Wing_Event",
    memory_type="event",
    intent_type="episode",
)

if set(_PREDICATES) != set(KG_PREDICATE_VALUES):
    missing = sorted(set(KG_PREDICATE_VALUES) - set(_PREDICATES))
    extra = sorted(set(_PREDICATES) - set(KG_PREDICATE_VALUES))
    raise RuntimeError(f"predicate projection registry mismatch: missing={missing}, extra={extra}")

_PREDICATES.update(
    {
        # Ledger-only identity for a durable natural-language assertion that
        # has no safe semantic triple. It is never written to the KG; Chroma is
        # its drawer projection and the canonical ledger owns lifecycle.
        "remembers_text": _definition("remembers_text"),
        "lives_in": _definition(
            "lives_in",
            cardinality=PredicateCardinality.SINGLE,
            temporality=PredicateTemporality.CURRENT_STATE,
            update_policy=PredicateUpdatePolicy.SUPERSEDE_EXPLICIT,
            projection_wing="Wing_Profile",
            projection_memory_type="profile",
        ),
        "born_in": _definition(
            "born_in",
            cardinality=PredicateCardinality.SINGLE,
            temporality=PredicateTemporality.DURABLE,
            update_policy=PredicateUpdatePolicy.REQUIRE_CORRECTION,
            projection_wing="Wing_Profile",
            projection_memory_type="profile",
        ),
        "has_state": _definition(
            "has_state",
            temporality=PredicateTemporality.CURRENT_STATE,
            projection_wing="Wing_Emotion",
            projection_memory_type="emotion",
        ),
        "has_emotion": _definition(
            "has_emotion",
            temporality=PredicateTemporality.CURRENT_STATE,
            projection_wing="Wing_Emotion",
            projection_memory_type="emotion",
        ),
        "has_symptom": _definition(
            "has_symptom",
            temporality=PredicateTemporality.CURRENT_STATE,
            sensitive=True,
            projection_wing="Wing_Health",
            projection_memory_type="health",
        ),
        "takes_medication": _definition(
            "takes_medication",
            temporality=PredicateTemporality.CURRENT_STATE,
            sensitive=True,
            projection_wing="Wing_Health",
            projection_memory_type="health",
        ),
        "has_health_condition": _definition(
            "has_health_condition",
            sensitive=True,
            projection_wing="Wing_Health",
            projection_memory_type="health",
        ),
        "attended": _definition(
            "attended",
            temporality=PredicateTemporality.EVENT,
            intent_type="episode",
            projection_wing="Wing_Event",
            projection_memory_type="event",
        ),
        "experienced": _definition(
            "experienced",
            temporality=PredicateTemporality.EVENT,
            intent_type="episode",
            projection_wing="Wing_Event",
            projection_memory_type="event",
        ),
        "achieved": _definition(
            "achieved",
            temporality=PredicateTemporality.EVENT,
            intent_type="episode",
            projection_wing="Wing_Event",
            projection_memory_type="event",
        ),
    }
)


def predicate_definition(predicate: str) -> PredicateDefinition:
    """Return product semantics for a wire predicate, failing closed."""

    try:
        return _PREDICATES[predicate]
    except KeyError as exc:
        raise ValueError(f"unsupported memory predicate: {predicate}") from exc


def predicate_definitions() -> tuple[PredicateDefinition, ...]:
    """Stable registry snapshot for validation and product introspection."""

    return tuple(_PREDICATES[predicate] for predicate in KG_PREDICATE_VALUES)


# ─── how a fact reads ────────────────────────────────────────────────────────
#
# These live beside the ontology rather than beside either caller because both
# paths need the same answer and used to disagree about it. The read path
# rendered a triple into Chinese through this table; the write path built the
# drawer that gets *embedded* with a bare f-string, so half of a palace's
# canonical drawers read "self owns pet:铁锤" while the deployed embedder is a
# Chinese model being asked to match "我家狗多大" against them. A fix on
# 2026-08-04 corrected the renderer and missed the writer, which is the half
# retrieval depends on.

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
_PREDICATE_TEMPLATES = {
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


def predicate_template(p: str) -> str:
    """The sentence shape for ``p``, with ``{s}``/``{o}`` for subject and object.

    An unknown predicate falls back to bare juxtaposition, which is ugly but
    still parseable — better than dropping the fact.
    """

    return _PREDICATE_TEMPLATES.get(p, "{s} " + p + " {o}")


#: The graph stores the owner as the literal subject ``self``. Left untranslated
#: it reaches the model as "self 计划 去日本" — a schema token presented as part
#: of a fact about the user.
SELF_LABEL = "用户"


def entity_label(value: object) -> str:
    text = str(value or "")
    if text == "self":
        return SELF_LABEL
    prefix, sep, label = text.partition(":")
    if sep and prefix.isascii() and prefix.replace("_", "").isalnum() and label:
        return label
    return text


def fact_sentence(subject: object, predicate: str, object_: object) -> str:
    """One statement as the sentence a person would say to mean it.

    This is what a drawer stores and what the ``[MEMORY]`` block renders, and
    those must be the same string: the drawer is embedded and matched against
    the user's own wording, so a schema token in it is a fact the retriever
    cannot find and the model reads as noise.
    """

    s = entity_label(subject)
    o = entity_label(object_)
    if predicate == "holds_role":
        # A breed is not a job. The graph uses one predicate for both because
        # the distinction is about the subject, not the relation.
        if str(subject).startswith("pet:"):
            return f"{s} 的品种/身份是 {o}"
        return f"{s} 的角色/身份是 {o}"
    return predicate_template(predicate).format(s=s, o=o)
