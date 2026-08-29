"""Wire contracts for talking to the Eidolon memory service.

The service splits along how urgently a caller needs an answer: reads are
synchronous and never raise (:class:`MemoryReadContract`), writes are published
asynchronously except when the caller must be truthful about what was stored
(:class:`MemoryWriteContract`).

Nothing here describes how memory is stored, ranked or fused. A client cannot
tell from this contract whether the service keeps a knowledge graph, which
vector backend it uses, or whether it runs locally or in a cluster.

**Not everything exported here is a promise to outside callers, and the
difference is worth stating because it was misread once.** Seventy symbols
leave this package; eleven are imported by any other repository, all of them by
``eidolon_agent`` and all of them about publishing a turn. The rest fall into
three groups:

* **Reachable from the two Protocols** — every type in a ``MemoryReadContract``
  or ``MemoryWriteContract`` signature. A client implementing or calling either
  needs them whether or not one does today. Fifteen of these.
* **Wire format** — subjects, stream patterns, the envelope, the parsers,
  ``memory_space_id`` derivation. Anything that publishes to or reads from the
  bus needs them, in any language.
* **Service-to-service commands** — ``KgAddTripleCommand``,
  ``KgInvalidateCommand``, ``PrivacyMutationCommand``,
  ``ConsolidatorIngestThemeCommand``. These are **built and consumed entirely
  inside eidolon_memory**: its own MCP tools publish them and its own turn
  worker applies them. They live here only because they are members of the
  ``MemoryCommandPayload`` discriminated union that ``parse_memory_command``
  dispatches on, and pulling them out would split the union rather than clean
  anything.

  The cost of that is real and was paid during this work: changing
  ``PrivacyMutationCommand`` looked like a cross-repository event and was
  hesitated over for exactly as long as it took to check that no other
  repository has ever referenced it. **Adding a field to one of these four is a
  local change.** Nothing outside this service sends them.
"""

from .audience import (
    OWNER_AUDIENCE,
    audience_companion_id,
    companion_audience,
    council_audience,
    is_companion_audience,
    readable_audiences,
    validate_audience,
)
from .envelope import (
    MEMORY_SCHEMA_VERSION,
    MemoryEnvelope,
    envelope_memory_payload,
    memory_payload_kind,
    parse_conversation_turn,
    parse_memory_command,
    unwrap_memory_payload,
)
from .intent import (
    MemoryIntent,
    MemoryIntentAuthority,
    MemoryIntentOperation,
    MemoryIntentType,
)
from .kg import (
    KG_PREDICATE_VALUES,
    SENSITIVE_PREDICATES,
    USER_CONFIRMED_ROOM_PREFIX,
    ConsolidatorIngestThemeCommand,
    DeviceSyncBatchPayload,
    DeviceSyncEvent,
    KgAddTripleCommand,
    KgInvalidateCommand,
    KgPredicate,
    MemoryCommandPayload,
    MemoryIntentCommand,
    PrivacyMutationCommand,
)
from .payloads import (
    ConversationTurnPayload,
    MemoryActorContext,
    build_memory_actor_context,
)
from .read import MemoryReadContract
from .results import (
    ActiveCommitment,
    CommitmentReadResult,
    ForgetAction,
    ForgetCandidate,
    ForgetOutcome,
    ForgetPreview,
    MemorySnippet,
    RecallPlan,
    RecallResult,
    SearchResult,
    ServiceStatus,
    SourceTurnLookup,
    TurnPublishReceipt,
    WriteOutcome,
    WriteStatus,
)
from .runtime_route import (
    DEFAULT_MEMORY_MCP_BASE_PORT,
    DEFAULT_MEMORY_MCP_HOST,
    DEFAULT_MEMORY_MCP_PATH,
    MEMORY_MCP_PORT_SPAN,
    MemoryRuntimeRoute,
    memory_runtime_route_for_realm,
    stable_memory_realm_port,
)
from .snapshot import (
    LEDGERS_PREFIX,
    PALACE_PREFIX,
    REQUIRED_ENTRIES,
    SNAPSHOT_CONTRACT_VERSION,
    RealmSnapshot,
    SnapshotEntry,
)
from .subjects import (
    MEMORY_COMMAND_BASE,
    MEMORY_CONVERSATION_TURN_BASE,
    MEMORY_SYNC_BASE,
    all_memory_stream_patterns,
    conversation_turn_stream_pattern,
    conversation_turn_subject,
    derive_memory_space_id,
    memory_command_stream_pattern,
    memory_command_subject,
    memory_space_storage_name,
    memory_space_subject_token,
    memory_sync_stream_pattern,
    memory_sync_subject,
    validate_memory_space_id,
)
from .write import MemoryWriteContract

__all__ = [
    "KG_PREDICATE_VALUES",
    "OWNER_AUDIENCE",
    "DEFAULT_MEMORY_MCP_BASE_PORT",
    "DEFAULT_MEMORY_MCP_HOST",
    "DEFAULT_MEMORY_MCP_PATH",
    "MEMORY_COMMAND_BASE",
    "MEMORY_CONVERSATION_TURN_BASE",
    "MEMORY_MCP_PORT_SPAN",
    "MEMORY_SYNC_BASE",
    "MEMORY_SCHEMA_VERSION",
    "SENSITIVE_PREDICATES",
    "USER_CONFIRMED_ROOM_PREFIX",
    "ActiveCommitment",
    "CommitmentReadResult",
    "ConversationTurnPayload",
    "ConsolidatorIngestThemeCommand",
    "DeviceSyncBatchPayload",
    "DeviceSyncEvent",
    "ForgetAction",
    "ForgetCandidate",
    "ForgetOutcome",
    "ForgetPreview",
    "KgAddTripleCommand",
    "KgInvalidateCommand",
    "MemoryEnvelope",
    "MemoryActorContext",
    "MemoryReadContract",
    "MemoryRuntimeRoute",
    "LEDGERS_PREFIX",
    "PALACE_PREFIX",
    "REQUIRED_ENTRIES",
    "SNAPSHOT_CONTRACT_VERSION",
    "RealmSnapshot",
    "SnapshotEntry",
    "MemorySnippet",
    "MemoryWriteContract",
    "KgPredicate",
    "MemoryCommandPayload",
    "MemoryIntentCommand",
    "MemoryIntent",
    "MemoryIntentAuthority",
    "MemoryIntentOperation",
    "MemoryIntentType",
    "PrivacyMutationCommand",
    "RecallPlan",
    "RecallResult",
    "SearchResult",
    "ServiceStatus",
    "SourceTurnLookup",
    "TurnPublishReceipt",
    "WriteOutcome",
    "WriteStatus",
    "all_memory_stream_patterns",
    "audience_companion_id",
    "build_memory_actor_context",
    "companion_audience",
    "council_audience",
    "conversation_turn_stream_pattern",
    "conversation_turn_subject",
    "derive_memory_space_id",
    "envelope_memory_payload",
    "is_companion_audience",
    "memory_payload_kind",
    "memory_runtime_route_for_realm",
    "memory_command_stream_pattern",
    "memory_command_subject",
    "memory_space_storage_name",
    "memory_space_subject_token",
    "memory_sync_stream_pattern",
    "memory_sync_subject",
    "parse_conversation_turn",
    "parse_memory_command",
    "readable_audiences",
    "stable_memory_realm_port",
    "unwrap_memory_payload",
    "validate_audience",
    "validate_memory_space_id",
]
