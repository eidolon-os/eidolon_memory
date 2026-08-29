"""One fail-closed policy for interaction memory visibility."""

from __future__ import annotations

from eidolon_memory_contracts import OWNER_AUDIENCE, companion_audience, council_audience


class MissingInteractionIdentity(ValueError):
    """An ordinary interaction cannot be assigned a safe audience."""


def interaction_audience(
    context: object,
    *,
    allow_owner_shared: bool = False,
) -> str:
    """Return one authoritative interaction audience.

    Owner is never the fallback for a malformed ordinary turn.  System/admin
    flows that intentionally materialise Owner Shared must opt in at their
    callsite, where that authority can be reviewed.
    """

    council_id = str(getattr(context, "council_id", "") or "").strip()
    if council_id:
        return council_audience(council_id)
    companion_id = str(getattr(context, "companion_id", "") or "").strip()
    if companion_id:
        return companion_audience(companion_id)
    if allow_owner_shared:
        return OWNER_AUDIENCE
    raise MissingInteractionIdentity(
        "interaction requires an authoritative companion_id or council_id"
    )


def derived_triple_audience(predicate: str, context: object) -> str:
    """Keep every ordinary derived projection in its interaction audience."""

    del predicate
    return interaction_audience(context)
