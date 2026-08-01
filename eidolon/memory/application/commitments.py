"""Project explicit canonical commitments through existing Realm write ports."""

from __future__ import annotations

from typing import Any

from eidolon_memory_contracts import OWNER_AUDIENCE, MemoryIntentCommand

from eidolon.memory.application.ingest import ingest_memory_fragment
from eidolon.memory.domain.commitment import ACTIVE_COMMITMENT_STATUSES
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import CommitmentWriter


async def apply_explicit_commitment(
    backend: Any,
    kg: Any,
    cmd: MemoryIntentCommand,
    commitments: CommitmentWriter,
) -> str:
    """Persist one commitment revision and repair its current projections."""

    intent = cmd.intent
    result = await commitments.apply(intent)
    record = result.commitment
    current_projection_id = f"{record.commitment_id}:revision:{record.revision}"
    previous_projection_id = (
        f"{record.commitment_id}:revision:{record.revision - 1}"
        if record.revision > 1
        else None
    )
    if previous_projection_id is not None:
        previous = await backend.get_by_source_turn_id(
            intent.memory_space_id,
            previous_projection_id,
        )
        if previous is not None and previous.metadata.get("privacy") != "do_not_recall":
            archived = await backend.archive_many(
                intent.memory_space_id,
                [previous.key],
            )
            if previous.key not in archived:
                raise RuntimeError("previous commitment drawer archive was not verified")

    active = record.status in ACTIVE_COMMITMENT_STATUSES
    if active:
        current = await backend.get_by_source_turn_id(
            intent.memory_space_id,
            current_projection_id,
        )
        if current is None:
            await ingest_memory_fragment(
                backend,
                MemoryFragment(
                    memory_id=current_projection_id,
                    memory_space_id=intent.memory_space_id,
                    memory_realm_id=intent.memory_space_id,
                    scope="persona",
                    visibility="all_devices",
                    source_device_id="admin",
                    source_instance_id=cmd.issuer,
                    wing="Wing_Future",
                    room=_commitment_room(record.commitment_id, record.revision),
                    content=intent.raw_claim,
                    memory_type="commitment",
                    importance=5,
                    confidence=intent.confidence,
                    occurred_at=intent.occurred_at or cmd.issued_at,
                    source_turn_id=current_projection_id,
                    session_id="user-confirmed",
                    tags=["user-confirmed", "commitment", record.status],
                    privacy="normal",
                    metadata={
                        "source": "user-confirmed",
                        "request_id": cmd.request_id,
                        "intent_id": intent.intent_id,
                        "commitment_id": record.commitment_id,
                        "commitment_status": record.status,
                        "commitment_revision": record.revision,
                    },
                ),
            )
        await commitments.mark_projected(
            intent.memory_space_id,
            record.commitment_id,
            record.revision,
            targets={"drawer"},
        )

    triples = await kg.query_entity(
        record.promisor,
        audiences=(OWNER_AUDIENCE,),
        direction="outgoing",
        include_sensitive=True,
    )
    kg_visible = any(
        row.subject == record.promisor
        and row.predicate == record.predicate
        and row.object == record.action
        for row in triples
    )
    if active and not kg_visible:
        await kg.add_triple(
            audience=OWNER_AUDIENCE,
            subject=record.promisor,
            predicate=record.predicate,
            object=record.action,
            valid_from=intent.occurred_at or cmd.issued_at,
            # A due date is not the end of validity. Overdue commitments stay
            # current until an explicit fulfilled/cancelled/superseded event.
            valid_to=None,
            confidence=intent.confidence,
            source_turn_id=record.commitment_id,
            adapter_name="commitment",
        )
    elif not active and kg_visible:
        await kg.invalidate(
            subject=record.promisor,
            predicate=record.predicate,
            object=record.action,
            ended=intent.occurred_at or cmd.issued_at,
        )
    await commitments.mark_projected(
        intent.memory_space_id,
        record.commitment_id,
        record.revision,
        targets={"kg"},
    )
    if not active:
        await commitments.mark_projected(
            intent.memory_space_id,
            record.commitment_id,
            record.revision,
            targets={"drawer"},
        )
    return current_projection_id


def _commitment_room(commitment_id: str, revision: int) -> str:
    return f"Commitment_{commitment_id[-16:]}_{revision}"
