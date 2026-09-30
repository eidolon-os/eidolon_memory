"""The Owner-facing HTTP surface of a memory realm, declared once.

Every route a Host calls on behalf of a person — status, browse, entries,
export, graph, recollections, and the three steps of forgetting — answers with
one of these models. The realm serializes through them and the Host parses with
them, so the two can no longer disagree.

That sentence is the reason this module exists. Until 2026-09-22 the realm wrote
these answers as hand-built dicts and the Host restated each shape as its own
strict model. The two drifted on the first day (``drawer_id``/``preview`` on one
side, ``id``/``text`` on the other) and each side's tests faked the other in the
Host's shape, so every suite was green while "forget" failed on every real
match. One declaration makes that class of defect a type error in whichever
repository changes it.

Strict on purpose: ``extra="forbid"`` on every model. A field the realm starts
sending is a change to this file, which both processes pick up from the same
release, rather than a surprise one of them rejects at runtime.

Not re-exported from the package root. The root namespace belongs to the
Agent-facing read/write contract, which has its own ``ForgetPreview`` with a
different audience and vocabulary; importing ``eidolon_memory_contracts.owner``
makes it explicit which surface a caller is speaking.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

OWNER_CONTRACT_VERSION = "1"

MaterializationState = Literal["ready", "materializing", "degraded", "unavailable"]
#: The command ledger's vocabulary for an asynchronous write, relayed unchanged.
#: ``applied`` is the only word that means the memory is gone.
ForgetState = Literal["accepted", "retrying", "applied", "failed"]


class OwnerWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Status — operator diagnostics for the cockpit, not an Owner surface.
# ---------------------------------------------------------------------------


class MemoryStatus(OwnerWireModel):
    """Whether this realm's projections have caught up with its ledger.

    Read by Mission Control. Deliberately not carried to a person's library:
    ``projection_pending`` is something only an operator can act on, and a phone
    that showed it would show 「正在整理」 forever for a projection that failed.
    """

    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.status"] = "memory.status"
    memory_realm_id: str = Field(min_length=1)
    memory_space_id: str = Field(min_length=1)
    audience_scope: str = Field(min_length=1)
    ready: bool
    data_readable: bool
    materialization_state: MaterializationState
    projection_pending: int = Field(ge=0)
    last_materialized_at: str | None = None
    degraded_reason: str = ""


# ---------------------------------------------------------------------------
# Browse — the library.
# ---------------------------------------------------------------------------


class MemoryRoom(OwnerWireModel):
    room_id: str = Field(min_length=1)
    drawer_count: int = Field(ge=0)
    #: A few titles, enough to recognise the room. Not the contents — a browse
    #: that returned everything would be an export by another name.
    drawers_preview: tuple[dict[str, Any], ...] = ()
    preview_truncated: bool = False


class MemoryWing(OwnerWireModel):
    wing_id: str = Field(min_length=1)
    is_configured: bool
    display_name: str = ""
    description: str = ""
    sort_order: int
    room_count: int = Field(ge=0)
    drawer_count: int = Field(ge=0)
    rooms: tuple[MemoryRoom, ...] = ()


class MemoryBrowse(OwnerWireModel):
    """What an Owner's memory holds, by wing and room.

    ``audience_scope`` is ``owner`` when the Owner asked for their own memory —
    every audience in their realm — and ``companion:<id>`` when they asked what
    one Companion can recall.
    """

    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.browse"] = "memory.browse"
    memory_space_id: str = Field(min_length=1)
    audience_scope: str = Field(min_length=1)
    wings: tuple[MemoryWing, ...] = ()
    entry_count: int = Field(ge=0)
    #: Present and not listed: the privacy wing, anything forgotten into the
    #: archive, anything scoped to one device — and, in a Companion view, what
    #: that Companion was not told.
    withheld_count: int = Field(ge=0)
    truncated: bool


# ---------------------------------------------------------------------------
# Graph.
# ---------------------------------------------------------------------------


class MemoryGraphNode(OwnerWireModel):
    node_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    degree: int = Field(ge=0)


class MemoryGraphEdge(OwnerWireModel):
    edge_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    recorded_at: str = ""


class MemoryGraph(OwnerWireModel):
    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.graph"] = "memory.graph"
    memory_space_id: str = Field(min_length=1)
    nodes: tuple[MemoryGraphNode, ...] = ()
    edges: tuple[MemoryGraphEdge, ...] = ()
    truncated: bool


# ---------------------------------------------------------------------------
# Entries — a window of days, newest first.
# ---------------------------------------------------------------------------


class MemoryEntry(OwnerWireModel):
    entry_id: str = Field(min_length=1)
    recorded_at: str = Field(min_length=1)
    #: Which field the time came from. "It filed this under yesterday" is a real
    #: complaint and this is what makes it answerable.
    recorded_at_source: str = ""
    wing_id: str = ""
    room_id: str = ""
    preview: str = ""


class MemoryEntries(OwnerWireModel):
    """What was recorded at or after ``since``, newest first, one page at a time.

    ``next_cursor`` is how a client reaches older entries: it sends it back as
    ``cursor``. Opaque, and a keyset over (time, entry id) rather than a time —
    facts from one turn share a timestamp, and a time bound would skip the ones
    tied with the last entry shown. Without paging, a window whose newest page
    was full could never show anything older.
    """

    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.entries"] = "memory.entries"
    memory_space_id: str = Field(min_length=1)
    since: str = Field(min_length=1)
    entries: tuple[MemoryEntry, ...] = ()
    entry_count: int = Field(ge=0)
    #: The page ended inside the window. Distinct from ``truncated``, which is
    #: the palace scan stopping.
    more_in_window: bool
    #: Present exactly when ``more_in_window``.
    next_cursor: str | None = None
    undated_count: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def _cursor_matches_more(self) -> Self:
        if self.more_in_window != (self.next_cursor is not None):
            raise ValueError("next_cursor is present exactly when more_in_window")
        return self


# ---------------------------------------------------------------------------
# Export — the person's own copy.
# ---------------------------------------------------------------------------


class MemoryExportRecord(OwnerWireModel):
    """One memory, whole. No length cap on ``value``: this is the copy."""

    entry_id: str = Field(min_length=1)
    recorded_at: str = ""
    recorded_at_source: str = ""
    wing_id: str = ""
    room_id: str = ""
    memory_type: str = ""
    #: Who was told. An Owner export carries every audience in the realm.
    audience: str = Field(min_length=1)
    value: str


class MemoryExport(OwnerWireModel):
    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.export"] = "memory.export"
    memory_space_id: str = Field(min_length=1)
    taken_at: str = Field(min_length=1)
    records: tuple[MemoryExportRecord, ...] = ()
    record_count: int = Field(ge=0)
    undated_count: int = Field(ge=0)
    truncated: bool


# ---------------------------------------------------------------------------
# Recollections — "what do you remember about …".
# ---------------------------------------------------------------------------


class MemoryRecollection(OwnerWireModel):
    text: str
    remembered_at: str | None = None


class MemoryRecollections(OwnerWireModel):
    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.recollections"] = "memory.recollections"
    memory_space_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    recollections: tuple[MemoryRecollection, ...] = ()


# ---------------------------------------------------------------------------
# Forgetting — preview, confirm, and where the confirmed change got to.
# ---------------------------------------------------------------------------


class OwnerForgetEntry(OwnerWireModel):
    """One memory the words matched.

    ``entry_id`` is a drawer id or a commitment id; a client passes it nowhere
    and needs it only as a stable key.
    """

    entry_id: str = Field(min_length=1)
    preview: str = ""
    #: 1.0 is an exact match; lower means the realm is guessing.
    score: float = Field(ge=0.0, le=1.0)


class OwnerForgetPreview(OwnerWireModel):
    """What forgetting these words would remove, before anything moves.

    There is no ``action``. An Owner forgets by deleting: the archive the realm
    also knows has no way back in the product, so offering it would present a
    permanent change as a reversible one.

    ``status`` carries three answers a client handles differently, and the
    validator below makes the combinations that would contradict them
    unrepresentable — a ``preview`` always has entries and a token, the other
    two never do.
    """

    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.forget-preview"] = "memory.forget-preview"
    status: Literal["preview", "not_found", "too_broad"]
    target: str = Field(min_length=1)
    entries: tuple[OwnerForgetEntry, ...] = ()
    #: More than one match, or an inexact one.
    needs_confirmation: bool = False
    #: Opaque and signed by the realm. Nothing above the realm parses it.
    confirmation_token: str | None = None
    #: Unix seconds after which the token is refused.
    expires_at: int | None = None
    #: Why nothing is offered, when ``too_broad``.
    detail: str = ""

    @model_validator(mode="after")
    def _status_matches_payload(self) -> Self:
        offered = bool(self.entries) or self.confirmation_token is not None
        if self.status == "preview":
            if not self.entries or not self.confirmation_token or self.expires_at is None:
                raise ValueError("a preview carries entries, a token and its expiry")
        elif offered:
            raise ValueError(f"a {self.status} answer offers nothing to confirm")
        return self


class OwnerForgetOutcome(OwnerWireModel):
    """What a confirm did.

    ``request_id`` is derived from the preview the token came from, so the same
    token confirmed twice names the same change and is answered from the ledger
    instead of being applied a second time.
    """

    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.forget-confirm"] = "memory.forget-confirm"
    request_id: str = Field(min_length=1)
    status: ForgetState
    target: str = Field(min_length=1)
    entry_count: int = Field(ge=0)


class OwnerForgetProgress(OwnerWireModel):
    """Where a confirmed forget has got to, read from the command ledger."""

    contract_version: Literal["1"] = OWNER_CONTRACT_VERSION
    operation: Literal["memory.forget-status"] = "memory.forget-status"
    request_id: str = Field(min_length=1)
    status: ForgetState


__all__ = [
    "OWNER_CONTRACT_VERSION",
    "ForgetState",
    "MaterializationState",
    "MemoryBrowse",
    "MemoryEntries",
    "MemoryEntry",
    "MemoryExport",
    "MemoryExportRecord",
    "MemoryGraph",
    "MemoryGraphEdge",
    "MemoryGraphNode",
    "MemoryRecollection",
    "MemoryRecollections",
    "MemoryRoom",
    "MemoryStatus",
    "MemoryWing",
    "OwnerForgetEntry",
    "OwnerForgetOutcome",
    "OwnerForgetPreview",
    "OwnerForgetProgress",
    "OwnerWireModel",
]
