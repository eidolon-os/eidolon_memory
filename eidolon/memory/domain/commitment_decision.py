"""What applying a commitment intent should do, given what is already stored.

This is the correctness content of the commitment ledger: which status may follow
which, how a revision merges with the one before it, and when a request is a
conflict rather than an update. None of it depends on where the rows live.

It sits here rather than inside a storage class because there are two storages. A
copy in each would be two implementations of one state machine, and they would
drift in the way that is hardest to notice — both plausible, disagreeing only on
an input nobody tested. So each implementation reads, calls this, and writes.

Pure by construction: no I/O, no clock. ``now`` is passed in, so the same inputs
always produce the same decision and a test can assert on it without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eidolon_memory_contracts import MemoryIntent

from eidolon.memory.domain.commitment import (
    CommitmentConflict,
    CommitmentRecord,
    CommitmentStatus,
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"proposed", "confirmed", "cancelled", "superseded"}),
    "confirmed": frozenset({"confirmed", "fulfilled", "cancelled", "superseded"}),
    "fulfilled": frozenset({"fulfilled"}),
    "cancelled": frozenset({"cancelled"}),
    "superseded": frozenset({"superseded"}),
}
"""Which status may follow which.

The three terminal states only map to themselves: a fulfilled promise does not
become proposed again because a later turn mentioned it. Re-applying the same
terminal status is allowed so that a redelivered intent is idempotent rather than
a conflict.
"""


@dataclass(frozen=True)
class CommitmentDecision:
    """The write a storage should perform, already decided."""

    record: CommitmentRecord
    previous_status: CommitmentStatus | None
    created: bool


def decide_commitment_apply(
    intent: MemoryIntent,
    *,
    existing: CommitmentRecord | None,
    fields: dict[str, Any],
    now: str,
    merge_values,
    validate_identity,
) -> CommitmentDecision:
    """Resolve an intent against stored state, or raise if it cannot apply.

    ``merge_values`` and ``validate_identity`` are passed in rather than imported
    so this module stays free of the storage layer it is called from; both are
    pure helpers there.

    Raises :class:`CommitmentConflict` for a request that cannot be honoured —
    targeting a commitment that does not exist, updating one that was never
    created, or a transition the state machine forbids. Each is a caller error
    that should surface rather than be applied approximately.
    """

    identity_id = fields["identity_id"]
    commitment_id = intent.target_id or identity_id

    if intent.target_id and existing is None:
        raise CommitmentConflict("target commitment does not exist")
    if existing is None and intent.operation_hint not in {"add", "confirm", None}:
        raise CommitmentConflict("commitment update requires an existing target")

    if existing is None:
        return CommitmentDecision(
            record=CommitmentRecord(
                commitment_id=commitment_id,
                memory_space_id=intent.memory_space_id,
                promisor=fields["promisor"],
                predicate=fields["predicate"],
                action=fields["action"],
                beneficiaries=fields["beneficiaries"],
                participants=fields["participants"],
                condition=fields["condition"],
                due_at=fields["due_at"],
                # An explicit confirmation skips the proposed step: the user has
                # already said it, so recording it as merely proposed would
                # understate what happened.
                status="confirmed" if intent.operation_hint == "confirm" else "proposed",
                revision=1,
                created_at=now,
                updated_at=now,
            ),
            previous_status=None,
            created=True,
        )

    beneficiaries = (
        fields["beneficiaries"] if fields["beneficiaries_provided"] else existing.beneficiaries
    )
    fields["beneficiaries"] = beneficiaries
    validate_identity(existing, intent, fields)

    previous_status = existing.status
    status = fields["requested_status"]
    if status not in TRANSITIONS[previous_status]:
        raise CommitmentConflict(
            f"invalid commitment transition: {previous_status} -> {status}"
        )

    return CommitmentDecision(
        record=CommitmentRecord(
            commitment_id=commitment_id,
            memory_space_id=intent.memory_space_id,
            promisor=fields["promisor"],
            predicate=fields["predicate"],
            action=fields["action"],
            beneficiaries=beneficiaries,
            # Participants accumulate: someone mentioned in an earlier revision is
            # still involved even when this turn does not name them.
            participants=merge_values(existing.participants, fields["participants"]),
            # Condition and due date replace, and only when this intent supplied
            # them — absent means "unchanged", not "cleared".
            condition=(
                fields["condition"]
                if "condition" in intent.attributes
                else existing.condition
            ),
            due_at=fields["due_at"] if "due_at" in intent.attributes else existing.due_at,
            status=status,
            revision=existing.revision + 1,
            created_at=existing.created_at,
            updated_at=now,
        ),
        previous_status=previous_status,
        created=False,
    )
