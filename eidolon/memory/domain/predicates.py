"""Product-owned predicate semantics for deterministic fact reconciliation.

The SDK whitelist controls which predicates may cross the wire.  This registry
adds the narrower business semantics that belong to the Memory domain.  An
unknown predicate therefore fails closed instead of inheriting an accidental
cardinality or update policy from an LLM prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from eidolon_memory_contracts import KG_PREDICATE_VALUES


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


def _definition(
    predicate: str,
    *,
    cardinality: PredicateCardinality = PredicateCardinality.MULTI,
    temporality: PredicateTemporality = PredicateTemporality.DURABLE,
    update_policy: PredicateUpdatePolicy = PredicateUpdatePolicy.EXACT_ONLY,
    sensitive: bool = False,
) -> PredicateDefinition:
    return PredicateDefinition(
        predicate=predicate,
        cardinality=cardinality,
        temporality=temporality,
        update_policy=update_policy,
        sensitive=sensitive,
    )


# Conservative by design.  Only ``lives_in`` currently has product semantics
# strong enough to replace another current value automatically, and even then
# only for an explicit ``update`` command.  Employment, roles, preferences,
# relationships, health facts and commitments may all legitimately be plural.
_PREDICATES: dict[str, PredicateDefinition] = {
    predicate: _definition(predicate) for predicate in KG_PREDICATE_VALUES
}
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
        ),
        "born_in": _definition(
            "born_in",
            cardinality=PredicateCardinality.SINGLE,
            temporality=PredicateTemporality.DURABLE,
            update_policy=PredicateUpdatePolicy.REQUIRE_CORRECTION,
        ),
        "has_state": _definition(
            "has_state", temporality=PredicateTemporality.CURRENT_STATE
        ),
        "has_emotion": _definition(
            "has_emotion", temporality=PredicateTemporality.CURRENT_STATE
        ),
        "has_symptom": _definition(
            "has_symptom",
            temporality=PredicateTemporality.CURRENT_STATE,
            sensitive=True,
        ),
        "takes_medication": _definition(
            "takes_medication",
            temporality=PredicateTemporality.CURRENT_STATE,
            sensitive=True,
        ),
        "has_health_condition": _definition(
            "has_health_condition", sensitive=True
        ),
        "attended": _definition(
            "attended", temporality=PredicateTemporality.EVENT
        ),
        "experienced": _definition(
            "experienced", temporality=PredicateTemporality.EVENT
        ),
        "achieved": _definition(
            "achieved", temporality=PredicateTemporality.EVENT
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
