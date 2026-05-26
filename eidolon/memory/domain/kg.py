"""Knowledge Graph domain types: predicates, triples, commands.

The whitelist of predicates is enforced via :data:`KgPredicate` (a typing
``Literal``); LLM steward output that uses anything else gets rejected by
pydantic and the whole decision is dropped (see plan §4.2). This is the
project's defence against KG predicate sprawl from LLM hallucination.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eidolon.memory.support.model_base import BaseEidolonModel

# ── Predicate whitelist (KG plan §4.2) ──────────────────────────────────────

KgPredicate = Literal[
    # 人际关系
    "child_of",
    "parent_of",
    "partner_of",
    "sibling_of",
    "friend_of",
    "colleague_of",
    # 身份 / 角色
    "works_at",
    "lives_in",
    "studies_at",
    "holds_role",
    "born_in",
    # 偏好
    "likes",
    "dislikes",
    "prefers",
    # 行为 / 活动
    "does",
    "practices",
    "owns",
    "uses",
    # 承诺 / 事项
    "promised",
    "committed_to",
    "planned_to",
    # 状态（时态性强）
    "has_state",
    "has_emotion",
    "has_concern",
    "worried_about",
    "struggles_with",
    # 健康（敏感）
    "has_health_condition",
    "takes_medication",
    "has_symptom",
    # 事件（一次性时刻）
    "attended",
    "experienced",
    "achieved",
]

SENSITIVE_PREDICATES: frozenset[str] = frozenset(
    {"has_health_condition", "takes_medication", "has_symptom"}
)

# Tuple form for pydantic / introspection / MCP doc tool
KG_PREDICATE_VALUES: tuple[str, ...] = tuple(
    getattr(KgPredicate, "__args__", ())  # type: ignore[attr-defined]
)


# ── Steward output schemas (parsed from LLM) ────────────────────────────────


class KgTripleAction(BaseEidolonModel):
    """One ``add_triple`` instruction emitted by the LLM steward."""

    subject: str = Field(min_length=1, max_length=128)
    predicate: KgPredicate
    object: str = Field(min_length=1, max_length=256)
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.9)


class KgInvalidationAction(BaseEidolonModel):
    """Steward output: stop a previously-valid triple at ``ended`` (or NOW)."""

    subject: str = Field(min_length=1)
    predicate: KgPredicate
    object: str = Field(min_length=1)
    ended: str | None = None
    reason: str = Field(default="", max_length=128)


# ── Read-side records (returned by LockedKnowledgeGraph) ────────────────────


class KgEntityRecord(BaseEidolonModel):
    """One entity returned by KG queries."""

    id: str
    name: str
    type: str = "unknown"


class KgTripleRecord(BaseEidolonModel):
    """One triple returned by KG queries (already resolved to display names)."""

    id: str
    subject: str
    predicate: str
    object: str
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = 1.0
    source_turn_id: str | None = None
    adapter_name: str | None = None


# ── NATS command payloads (admin / agent writes via JetStream) ─────────────


class _BaseMemoryCommand(BaseEidolonModel):
    """Common fields on every command published to ``agent.memory.cmd.<user>``.

    All admin / agent writes flow through the same JetStream pipeline that
    handles ``ConversationTurnPayload`` — this keeps the stream as the single
    source of truth for D5 rebuild and gives admin edits the same durability
    as chat-derived triples.
    """

    request_id: str
    user_id: str
    issued_at: str
    issuer: Literal["admin", "agent"] = "admin"


class KgAddTripleCommand(_BaseMemoryCommand):
    kind: Literal["kg_add_triple"] = "kg_add_triple"
    subject: str
    predicate: KgPredicate
    object: str
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    source_drawer_id: str | None = None
    adapter_name: str = "admin"


class KgInvalidateCommand(_BaseMemoryCommand):
    kind: Literal["kg_invalidate"] = "kg_invalidate"
    subject: str
    predicate: KgPredicate
    object: str
    ended: str | None = None


class ConsolidatorIngestThemeCommand(_BaseMemoryCommand):
    """Phase 4 — emitted by the ``eidolon-memory-consolidator`` worker.

    The consolidator is **another agent client** that:
      1. reads drawers via MCP (read-only, doesn't break D1 single-owner),
      2. asks an LLM to produce a small set of cross-time themes,
      3. publishes each surviving theme back via this cmd kind.

    The agent_runner's ``process_command_message`` writes the theme as a
    standard ``MemoryFragment`` in ``Wing_Theme``, **bypassing the steward**
    (themes are already structured, re-LLM extraction would loop). The
    ``request_id`` doubles as an idempotency key — re-publishing the same
    theme is a no-op at the backend layer (chromadb dedup on doc id).
    """

    kind: Literal["consolidator_ingest_theme"] = "consolidator_ingest_theme"
    text: str = Field(min_length=1)
    underlying_wing: str       # Source wing the consolidator distilled (e.g. "Wing_Work")
    window_days: int = Field(gt=0, default=30)
    source_drawer_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.7)


class UserConfirmedFactCommand(_BaseMemoryCommand):
    """Phase 5.2 — verbatim fact the user explicitly asked to remember.

    Why a dedicated cmd kind rather than routing through the conversation
    turn pipeline:

    - Steward (LLM) may **paraphrase, mis-classify, or drop** the user's
      statement; user-confirmed facts must land **verbatim**.
    - User-confirmed facts get a recall-time priority boost (pinned ahead
      of cosine-ranked drawers in their wing). The cmd kind is the marker
      that lets the recall path identify them at write time, not via
      brittle prompt-engineering of the steward.
    - Same idempotency story as the consolidator: ``request_id`` is the
      stable fragment id; redelivery collapses at chroma.

    Typical invokers:
      - LiveKit / chat agent recognizing "记住 ..." / "remember this"
        intents and calling the ``eidolon_memory_user_confirm`` MCP tool.
      - Admin UI surfacing a "pin this fact" affordance.
    """

    kind: Literal["user_confirm_fact"] = "user_confirm_fact"
    text: str = Field(min_length=1)
    wing: str                      # Destination wing (e.g. "Wing_Profile")
    memory_type: str = "profile"   # MemoryType literal; kept str for replay tolerance
    importance: int = Field(ge=1, le=5, default=5)
    confidence: float = Field(ge=0.0, le=1.0, default=0.99)
    tags: list[str] = Field(default_factory=list)


MemoryCommandPayload = (
    KgAddTripleCommand
    | KgInvalidateCommand
    | ConsolidatorIngestThemeCommand
    | UserConfirmedFactCommand
)
"""Discriminated union; route on the ``kind`` field."""
