"""Who a memory is for.

An owner may talk to several companions. Facts about the owner themselves —
preferences, biography, health — hold no matter which companion is listening, so
they belong to the owner and every companion may recall them. What happened
between the owner and one particular companion — a shared joke, a nickname, a
promise that companion made — belongs to that companion alone.

Audience is a visibility axis, not a security boundary between owners: an
owner's data is isolated from other owners by storage, before any audience
filter runs. It is also independent of sensitivity — a health fact is owner-wide
but still filtered out of ordinary recall by the sensitive-predicate rules.
"""

from __future__ import annotations

import re

OWNER_AUDIENCE = "owner"
_COMPANION_PREFIX = "companion:"
_COUNCIL_PREFIX = "council:"

# Companion ids come from the caller's token; keep the audience token safe to
# embed in metadata keys, SQL parameters and vector-store filter expressions.
_COMPANION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def companion_audience(companion_id: str) -> str:
    """Return the audience token for memories private to one companion."""

    value = (companion_id or "").strip()
    if not _COMPANION_ID_RE.fullmatch(value):
        raise ValueError(f"companion_id is not a safe audience token: {companion_id!r}")
    return f"{_COMPANION_PREFIX}{value}"


def council_audience(council_id: str) -> str:
    """Return the audience token for one participant-scoped Council session."""

    value = (council_id or "").strip()
    if not _COMPANION_ID_RE.fullmatch(value):
        raise ValueError(f"council_id is not a safe audience token: {council_id!r}")
    return f"{_COUNCIL_PREFIX}{value}"


def validate_audience(audience: str) -> str:
    """Validate an audience token, returning its canonical form."""

    value = (audience or "").strip()
    if value == OWNER_AUDIENCE:
        return value
    if value.startswith(_COMPANION_PREFIX):
        return companion_audience(value[len(_COMPANION_PREFIX) :])
    if value.startswith(_COUNCIL_PREFIX):
        return council_audience(value[len(_COUNCIL_PREFIX) :])
    raise ValueError(
        f"audience must be {OWNER_AUDIENCE!r} or "
        f"{_COMPANION_PREFIX}<companion_id> or {_COUNCIL_PREFIX}<council_id>; "
        f"got {audience!r}"
    )


def is_companion_audience(audience: str) -> bool:
    """True when the audience restricts a memory to a single companion."""

    return validate_audience(audience).startswith(_COMPANION_PREFIX)


def audience_companion_id(audience: str) -> str | None:
    """Return the companion id an audience is private to, if any."""

    value = validate_audience(audience)
    if not value.startswith(_COMPANION_PREFIX):
        return None
    return value[len(_COMPANION_PREFIX) :]


def readable_audiences(
    companion_id: str | None,
    *,
    council_id: str | None = None,
) -> tuple[str, ...]:
    """Audience tokens a companion may recall.

    Without a companion id the caller gets the owner layer only. That is the
    safe direction: an unidentified caller must never see what the owner shared
    with one specific companion.
    """

    readable = [OWNER_AUDIENCE]
    if (companion_id or "").strip():
        readable.append(companion_audience(companion_id))
    if (council_id or "").strip():
        readable.append(council_audience(council_id))
    return tuple(readable)
