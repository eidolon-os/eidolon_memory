"""Fail-closed projection of explicit canonical memory intents."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

from eidolon_memory_contracts import (
    KG_PREDICATE_VALUES,
    OWNER_AUDIENCE,
    USER_CONFIRMED_ROOM_PREFIX,
    MemoryIntent,
    MemoryIntentCommand,
)

from eidolon.memory.application.canonical_invalidation import (
    invalidate_exact_canonical_fact,
)
from eidolon.memory.application.claim_routing import route_explicit_claim
from eidolon.memory.application.commitments import apply_explicit_commitment
from eidolon.memory.application.ingest import ingest_memory_fragment
from eidolon.memory.application.scope_policy import (
    derived_triple_audience,
    interaction_audience,
)
from eidolon.memory.domain.canonical_fact import (
    CanonicalFactRegistration,
    ProjectionTarget,
)
from eidolon.memory.domain.commitment import CommitmentConflict
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import CanonicalFactWriter, CommitmentWriter
from eidolon.memory.domain.predicates import (
    PredicateCardinality,
    PredicateUpdatePolicy,
    predicate_definition,
)


class MemoryIntentRejected(ValueError):
    """A valid wire intent that this projection contract cannot safely apply."""


async def apply_explicit_intent(
    backend: Any,
    kg: Any,
    cmd: MemoryIntentCommand,
    canonical_facts: CanonicalFactWriter | None = None,
    commitments: CommitmentWriter | None = None,
) -> str:
    """Project one explicit intent without bypassing write ports.

    Adds/confirms create canonical projections. A correction is accepted only
    for a complete triple and uses the same exact invalidation lifecycle as
    automatic extraction. Natural-language update/forget remains fail-closed;
    privacy mutation stays on its preview/confirm protocol.
    """
    intent = cmd.intent
    if intent.authority not in {"explicit_user", "explicit_admin"}:
        raise MemoryIntentRejected(
            "memory_intent command requires explicit authority"
        )
    if intent.intent_type == "forget":
        raise MemoryIntentRejected(
            "forget intents require the exact privacy preview/confirm flow"
        )
    if intent.intent_type == "commitment":
        if commitments is None or kg is None:
            raise MemoryIntentRejected(
                "commitment intent requires commitment and KG ports"
            )
        try:
            return await apply_explicit_commitment(
                backend,
                kg,
                cmd,
                commitments,
            )
        except (CommitmentConflict, ValueError) as exc:
            raise MemoryIntentRejected(str(exc)) from exc
    if intent.intent_type == "correction":
        if (
            intent.operation_hint != "invalidate"
            or not intent.subject
            or not intent.predicate
            or not intent.object
        ):
            raise MemoryIntentRejected(
                "correction requires an exact subject/predicate/object invalidation"
            )
        if intent.predicate not in KG_PREDICATE_VALUES:
            raise MemoryIntentRejected(
                f"unsupported KG predicate: {intent.predicate}"
            )
        if kg is None or canonical_facts is None:
            raise MemoryIntentRejected(
                "exact correction requires KG and canonical fact ports"
            )
        stamped = (
            intent
            if intent.occurred_at is not None
            else intent.model_copy(update={"occurred_at": cmd.issued_at})
        )
        result = await invalidate_exact_canonical_fact(
            backend,
            kg,
            stamped,
            canonical_facts,
        )
        if not result.canonical_matched and result.kg_rows_invalidated == 0:
            already_applied = await kg.find_invalidation_applied(
                intent.subject,
                intent.predicate,
                intent.object,
                stamped.occurred_at,
            )
            if not already_applied:
                raise MemoryIntentRejected("exact correction matched no fact")
        return f"invalidated:{result.assertion_id}"
    structured = (intent.subject, intent.predicate, intent.object)
    if any(structured) and not all(structured):
        raise MemoryIntentRejected(
            "structured intent requires subject, predicate, and object"
        )
    if all(structured):
        if intent.predicate not in KG_PREDICATE_VALUES:
            raise MemoryIntentRejected(
                f"unsupported KG predicate: {intent.predicate}"
            )
        if kg is None:
            raise RuntimeError("structured memory intent requires KG backend")

    prepared_registration = None
    if intent.operation_hint == "update":
        if not all(structured) or canonical_facts is None:
            raise MemoryIntentRejected(
                "update requires a structured fact and canonical fact port"
            )
        prepared_registration = await _prepare_explicit_update(
            backend,
            kg,
            intent,
            canonical_facts,
            occurred_at=intent.occurred_at or cmd.issued_at,
        )
    elif intent.operation_hint not in {None, "add", "confirm"}:
        raise MemoryIntentRejected("unsupported memory intent operation")

    attributes = intent.attributes
    route = route_explicit_claim(
        intent.raw_claim,
        intent_type=intent.intent_type,
    )
    requested_wing = _non_blank_attribute(attributes, "wing", "auto")
    requested_memory_type = _non_blank_attribute(
        attributes, "memory_type", "auto"
    )
    wing = route.wing if requested_wing == "auto" else requested_wing
    memory_type = (
        route.memory_type
        if requested_memory_type == "auto"
        else requested_memory_type
    )
    importance = _bounded_int_attribute(attributes, "importance", 5, 1, 5)
    tags = _string_list_attribute(attributes, "tags")
    scope = attributes.get("scope", "persona")
    if scope not in {"global", "persona", "agent", "device", "session"}:
        scope = "persona"
    visibility = attributes.get("visibility", "all_devices")
    if visibility not in {"all_devices", "current_device", "private"}:
        visibility = "all_devices"
    source = (
        "user-confirmed"
        if intent.authority == "explicit_user"
        else "admin-confirmed"
    )
    extensions = attributes.get("extensions", {})
    if not isinstance(extensions, dict):
        extensions = {}
    # A verbatim confirmation is still part of one interaction. It is not an
    # implicit request to publish that conversation to every Companion. Stable
    # owner facts may still be promoted independently by the derived KG policy;
    # provenance and visibility must not be collapsed into one switch.
    interaction_context = SimpleNamespace(
        companion_id=_optional_attribute(attributes, "source_instance_id"),
        council_id=_optional_attribute(attributes, "council_id"),
    )
    drawer_audience = interaction_audience(interaction_context)
    registration = prepared_registration
    projection_identity = intent.intent_id
    pending_targets: set[ProjectionTarget] = {"drawer"}
    if all(structured):
        pending_targets.add("kg")
    if all(structured) and canonical_facts is not None:
        requested_targets: set[ProjectionTarget] = {"drawer", "kg"}
        if registration is None:
            registration = await canonical_facts.register(
                intent,
                targets=requested_targets,
            )
        if registration.state != "active" and not registration.reactivation_pending:
            return f"{registration.state}:{registration.assertion_id}"
        projection_identity = registration.projection_id or registration.assertion_id
        pending_targets = set(registration.pending_targets)
        projected_targets = requested_targets - pending_targets
        targets_to_verify = set(projected_targets)
        if not registration.evidence_created or registration.evidence_count > 1:
            targets_to_verify.update(pending_targets)
        if targets_to_verify:
            visible_targets = await _visible_canonical_targets(
                backend,
                kg,
                intent,
                projection_identity,
                targets_to_verify,
            )
            missing_targets = projected_targets - visible_targets
            if missing_targets:
                await canonical_facts.mark_projection_pending(
                    intent.memory_space_id,
                    registration.assertion_id,
                    targets=missing_targets,
                )
                pending_targets.update(missing_targets)
            recovered_targets = pending_targets & visible_targets
            if recovered_targets:
                await canonical_facts.mark_projected(
                    intent.memory_space_id,
                    registration.assertion_id,
                    targets=recovered_targets,
                )
                pending_targets.difference_update(recovered_targets)
            if not pending_targets:
                if registration.reactivation_pending:
                    await canonical_facts.mark_reactivated(
                        intent.memory_space_id,
                        registration.intent_id,
                    )
                    return f"reactivated:{registration.assertion_id}"
                return (
                    f"confirmed:{registration.assertion_id}:"
                    f"evidence:{registration.evidence_count}"
                )

    resource_id = f"memoryintent:{projection_identity}"
    if "drawer" in pending_targets:
        fragment = MemoryFragment(
            memory_id=resource_id,
            memory_space_id=cmd.memory_space_id,
            memory_realm_id=cmd.memory_space_id,
            companion_id=_optional_attribute(attributes, "source_instance_id"),
            audience=drawer_audience,
            scope=scope,
            visibility=visibility,
            source_device_id=(
                _optional_attribute(attributes, "source_device_id") or "admin"
            ),
            target_device_id=_optional_attribute(attributes, "target_device_id"),
            source_instance_id=(
                _optional_attribute(attributes, "source_instance_id") or cmd.issuer
            ),
            wing=wing,
            room=(
                f"{USER_CONFIRMED_ROOM_PREFIX}"
                f"{_projection_room_token(projection_identity)}"
            ),
            content=intent.raw_claim,
            memory_type=memory_type,
            importance=importance,
            confidence=intent.confidence,
            occurred_at=intent.occurred_at or cmd.issued_at,
            source_turn_id=(
                f"canonical:{projection_identity}"
                if registration is not None
                else intent.source_event_id
            ),
            session_id=_optional_attribute(attributes, "session_id") or source,
            tags=[source, *tags],
            privacy="normal",
            metadata={
                "source": source,
                "request_id": cmd.request_id,
                "intent_id": intent.intent_id,
                "intent_type": intent.intent_type,
                "authority": intent.authority,
                "tool_call_id": intent.tool_call_id or "",
            },
            extensions=extensions,
        )
        await ingest_memory_fragment(backend, fragment)
        if registration is not None:
            await canonical_facts.mark_projected(
                intent.memory_space_id,
                registration.assertion_id,
                targets={"drawer"},
            )

    if all(structured) and "kg" in pending_targets:
        await kg.add_triple(
            audience=derived_triple_audience(intent.predicate, interaction_context),
            subject=intent.subject,
            predicate=intent.predicate,
            object=intent.object,
            valid_from=intent.occurred_at or cmd.issued_at,
            valid_to=None,
            confidence=intent.confidence,
            source_turn_id=(
                f"canonical:{projection_identity}:evidence:{intent.intent_id}"
                if registration is not None
                else intent.source_event_id
            ),
            adapter_name=source,
        )
        if registration is not None:
            await canonical_facts.mark_projected(
                intent.memory_space_id,
                registration.assertion_id,
                targets={"kg"},
            )
    if registration is not None and registration.reactivation_pending:
        await canonical_facts.mark_reactivated(
            intent.memory_space_id,
            registration.intent_id,
        )
        return f"reactivated:{registration.assertion_id}"
    return resource_id


async def _prepare_explicit_update(
    backend: Any,
    kg: Any,
    intent: MemoryIntent,
    canonical_facts: CanonicalFactWriter,
    *,
    occurred_at: str,
) -> CanonicalFactRegistration | None:
    """Plan exact reactivation or end a replaceable current single slot.

    The caller has already enforced explicit authority and a complete triple.
    Exact inactive facts may be reactivated without guessing. A different
    object may be replaced only when the product registry explicitly permits
    single-slot supersession.
    """

    assert intent.subject is not None
    assert intent.predicate is not None
    assert intent.object is not None
    definition = predicate_definition(intent.predicate)
    exact = await canonical_facts.get_fact(
        intent.memory_space_id,
        intent.subject,
        intent.predicate,
        intent.object,
    )
    if exact is not None and exact.state == "active":
        return None

    if exact is not None:
        if definition.cardinality == PredicateCardinality.SINGLE:
            await _supersede_explicit_single_slot(
                backend,
                kg,
                intent,
                canonical_facts,
                occurred_at=occurred_at,
            )
        return await canonical_facts.register_reactivation(
            intent.model_copy(update={"occurred_at": occurred_at}),
            targets={"drawer", "kg"},
        )

    if (
        definition.cardinality != PredicateCardinality.SINGLE
        or definition.update_policy != PredicateUpdatePolicy.SUPERSEDE_EXPLICIT
    ):
        raise MemoryIntentRejected(
            f"predicate {intent.predicate} requires exact correction, not update"
        )
    await _supersede_explicit_single_slot(
        backend,
        kg,
        intent,
        canonical_facts,
        occurred_at=occurred_at,
    )
    return None


def _projection_room_token(projection_identity: str) -> str:
    if ":activation:" not in projection_identity:
        return projection_identity[-16:]
    return hashlib.sha256(projection_identity.encode("utf-8")).hexdigest()[:16]


async def _supersede_explicit_single_slot(
    backend: Any,
    kg: Any,
    intent: MemoryIntent,
    canonical_facts: CanonicalFactWriter,
    *,
    occurred_at: str,
) -> None:
    assert intent.subject is not None
    assert intent.predicate is not None
    assert intent.object is not None
    definition = predicate_definition(intent.predicate)
    if (
        definition.cardinality != PredicateCardinality.SINGLE
        or definition.update_policy != PredicateUpdatePolicy.SUPERSEDE_EXPLICIT
    ):
        active = await canonical_facts.active_for_slot(
            intent.memory_space_id,
            intent.subject,
            intent.predicate,
        )
        if any(fact.object != intent.object for fact in active):
            raise MemoryIntentRejected(
                f"predicate {intent.predicate} requires exact correction, not update"
            )
        return

    active = await canonical_facts.active_for_slot(
        intent.memory_space_id,
        intent.subject,
        intent.predicate,
    )
    different = [fact for fact in active if fact.object != intent.object]
    same = [fact for fact in active if fact.object == intent.object]
    if len(different) > 1 or (different and same):
        raise MemoryIntentRejected("single predicate slot has conflicting active facts")
    if not different:
        return

    old = different[0]
    invalidation = MemoryIntent(
        intent_id=f"{intent.intent_id}:supersede:{old.assertion_id}",
        memory_space_id=intent.memory_space_id,
        source_event_id=intent.source_event_id,
        authority=intent.authority,
        intent_type="correction",
        raw_claim=intent.raw_claim,
        operation_hint="invalidate",
        subject=old.subject,
        predicate=old.predicate,
        object=old.object,
        occurred_at=occurred_at,
        tool_call_id=intent.tool_call_id,
        confidence=intent.confidence,
        attributes={
            "reason": f"superseded by {intent.intent_id}",
            "result_state": "superseded",
            "superseded_by_intent_id": intent.intent_id,
        },
    )
    result = await invalidate_exact_canonical_fact(
        backend,
        kg,
        invalidation,
        canonical_facts,
    )
    if not result.canonical_matched:
        raise MemoryIntentRejected("single predicate update lost its active fact")


def _non_blank_attribute(attributes: dict[str, Any], key: str, default: str) -> str:
    value = attributes.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _optional_attribute(attributes: dict[str, Any], key: str) -> str | None:
    value = attributes.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _bounded_int_attribute(
    attributes: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = attributes.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, min(maximum, value))


def _string_list_attribute(attributes: dict[str, Any], key: str) -> list[str]:
    value = attributes.get(key)
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    )


async def _visible_canonical_targets(
    backend: Any,
    kg: Any,
    intent: MemoryIntent,
    assertion_id: str,
    targets: set[ProjectionTarget],
) -> set[ProjectionTarget]:
    visible: set[ProjectionTarget] = set()
    if "drawer" in targets:
        drawer = await backend.get_by_source_turn_id(
            intent.memory_space_id,
            f"canonical:{assertion_id}",
        )
        if drawer is not None:
            visible.add("drawer")
    if "kg" in targets:
        triples = await kg.query_entity(
            intent.subject,
            audiences=(OWNER_AUDIENCE,),
            direction="outgoing",
            include_sensitive=True,
        )
        if any(
            row.subject == intent.subject
            and row.predicate == intent.predicate
            and row.object == intent.object
            for row in triples
        ):
            visible.add("kg")
    return visible
