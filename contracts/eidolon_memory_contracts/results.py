"""Values exchanged across the memory service boundary.

These are the shapes a client sees. They deliberately do not mirror the
service's storage vocabulary — a caller gets a snippet with an id and text, not
a drawer with a key and a value — so that how memory is stored, ranked or fused
stays changeable without touching clients.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ._model import EidolonWireModel

WriteStatus = Literal["accepted", "retrying", "applied", "failed", "unknown"]
"""Truthful outcome of a write.

``applied`` is the only status that means the write is durable and readable.
``accepted`` means the service took the request and will retry on its own;
``unknown`` means we could not establish the outcome — a caller must not claim
success on either. Callers can resolve a non-terminal status later with the
returned ``request_id``.
"""

ForgetAction = Literal["archive", "delete"]


class RecallPlan(EidolonWireModel):
    """How a caller wants one recall performed.

    ``focus_subjects`` names entities the turn is about. It is a hint, not a
    directive: the service may use it to sharpen retrieval by whatever means it
    has, or ignore it entirely. Callers must not infer anything about the
    service's internals from whether it helps.
    """

    semantic_k: int = 5
    voice: bool = False
    focus_subjects: tuple[str, ...] = ()


class MemorySnippet(EidolonWireModel):
    """One recalled memory."""

    id: str
    text: str
    kind: str = "fragment"
    similarity: float = 0.0
    memory_time: datetime | None = None
    memory_time_source: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecallResult(EidolonWireModel):
    """Prompt-ready recall.

    ``context`` is the rendered block a caller injects into a prompt; it already
    carries everything the service decided was relevant, structured facts and
    recent turns included. ``snippets`` is the same material itemised, for
    callers that want to make their own presentation decisions.

    ``degraded_reason`` is for operator traces. Never put it in a prompt.
    """

    context: str = ""
    snippets: list[MemorySnippet] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None


class SearchResult(EidolonWireModel):
    """Explicit lookup, unranked by conversational policy."""

    snippets: list[MemorySnippet] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None


class WriteOutcome(EidolonWireModel):
    """Result of one explicit write."""

    status: WriteStatus
    request_id: str
    resource_id: str | None = None
    error: str | None = None

    @property
    def durable(self) -> bool:
        """True only when the write is stored and readable."""

        return self.status == "applied"


class TurnPublishReceipt(EidolonWireModel):
    """Acknowledgement that a completed turn was handed to the service.

    Publishing is asynchronous: ``published`` means the bus accepted the
    message, not that memory has absorbed it. Deduplication is by ``turn_id``,
    so republishing the same turn is safe.
    """

    turn_id: str
    memory_space_id: str
    state: Literal["published", "publish_failed", "skipped_no_bus"]
    subject: str | None = None
    error: str | None = None
    trace_id: str | None = None
    recorded_at: str = ""


class ForgetCandidate(EidolonWireModel):
    """A memory a natural-language privacy request might refer to."""

    id: str
    text: str
    score: float = 0.0


class ForgetPreview(EidolonWireModel):
    """What a privacy request resolves to, before anything is changed.

    Previewing never mutates. When ``requires_explicit_confirmation`` is set the
    caller must show the candidates to the user and pass
    ``confirmation_token`` back to commit — the token is scoped to exactly these
    candidates and expires.
    """

    status: Literal["preview", "not_found", "too_broad", "unavailable", "failed"]
    target: str
    action: ForgetAction
    candidates: list[ForgetCandidate] = Field(default_factory=list)
    requires_explicit_confirmation: bool = False
    confirmation_token: str = ""
    expires_at: str = ""
    error: str = ""


class ForgetOutcome(EidolonWireModel):
    """Result of committing a privacy request."""

    status: Literal["accepted", "applied", "failed", "unavailable"]
    action: ForgetAction
    request_id: str = ""
    forgotten_ids: list[str] = Field(default_factory=list)
    error: str = ""


class ActiveCommitment(EidolonWireModel):
    """A promise still in play."""

    commitment_id: str
    promisor: str
    predicate: Literal["promised", "committed_to", "planned_to"]
    action: str
    status: Literal["proposed", "confirmed"]
    beneficiaries: tuple[str, ...] = ()
    participants: tuple[str, ...] = ()
    condition: str | None = None
    due_at: str | None = None
    revision: int = 1
    updated_at: str = ""


class CommitmentReadResult(EidolonWireModel):
    """Bounded read of current commitments; terminal ones are never included."""

    commitments: list[ActiveCommitment] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    degraded: bool = False
    degraded_reason: str | None = None


class SourceTurnLookup(EidolonWireModel):
    """What the service absorbed from one published turn.

    Used to check whether a turn has been processed yet, and what it produced.
    """

    source_turn_id: str
    snippets: list[MemorySnippet] = Field(default_factory=list)
    found: bool = False
    degraded: bool = False
    degraded_reason: str | None = None


class ServiceStatus(EidolonWireModel):
    """Operational summary of one memory space.

    Fields beyond these are service-defined and carried in ``details``; clients
    must treat them as opaque diagnostics, never as capability flags to branch
    on.
    """

    memory_space_id: str
    ready: bool = False
    memory_count: int = 0
    details: dict[str, Any] = Field(default_factory=dict)
