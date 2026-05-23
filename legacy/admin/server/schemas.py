"""OpenAPI schemas for admin HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MemoryCreateRequest(BaseModel):
    user_id: str = Field(description="Agent runner user (NATS + palace routing).")
    wing: str = Field(description="Semantic wing id for steward ingest.")
    room: str = Field(description="Room id within the wing.")
    text: str
    metadata: dict[str, Any] | None = None


class MemorySearchResponse(BaseModel):
    records: list[dict[str, Any]]


class MemoryListResponse(BaseModel):
    records: list[dict[str, Any]]
    total_hint: int | None = Field(
        default=None,
        description="Row count for this page.",
    )


class UserStatusOut(BaseModel):
    user_id: str
    port: int
    enabled: bool = True
    palace_path: str
    mcp_http_url: str
    agent_reachable: bool = False
    runner_status: dict[str, Any] | None = None
    runner_status_error: str | None = None


class HealthResponse(BaseModel):
    ok: bool = True
    steward_mode: str
    default_user_id: str
    users: list[UserStatusOut]


class MemPalaceLayerInfo(BaseModel):
    level: str = Field(description="Machine id: palace | wing | room | drawer.")
    title: str = Field(description="Chinese short title for UI.")
    description: str


class HierarchyDrawerPreview(BaseModel):
    key: str
    preview: str


class HierarchyRoomOut(BaseModel):
    room_id: str
    drawer_count: int
    drawers_preview: list[HierarchyDrawerPreview]
    preview_truncated: bool = Field(
        ...,
        description="True when more drawers exist in this room than returned in previews.",
    )


class HierarchyWingOut(BaseModel):
    wing_id: str
    is_configured: bool
    display_name: str = ""
    description: str = ""
    sort_order: int = Field(999999, description="Lower appears first among configured wings.")
    room_count: int = 0
    drawer_count: int = 0
    rooms: list[HierarchyRoomOut]


class MemPalaceHierarchyResponse(BaseModel):
    palace_path: str
    layers: list[MemPalaceLayerInfo]
    room_naming_conventions: list[str]
    steward_mode: str
    total_records_scanned: int
    capped_by_max_records: bool
    configured_wings: list[HierarchyWingOut]
    extra_wings: list[HierarchyWingOut]


class GraphNodeOut(BaseModel):
    id: str
    label: str
    kind: str = Field(description="entity | room")
    entity_type: str | None = None
    wings: list[str] | None = None
    halls: list[str] | None = None
    count: int | None = None
    is_tunnel: bool | None = None


class GraphEdgeOut(BaseModel):
    id: str
    source: str
    target: str
    label: str = ""
    valid_from: str | None = None
    valid_to: str | None = None
    current: bool | None = None
    shared_wings: list[str] | None = None


class KnowledgeGraphSnapshot(BaseModel):
    available: bool
    palace_path: str
    kg_path: str
    stats: dict[str, Any] | None = None
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    capped: bool = False
    triple_count: int | None = None
    reason: str | None = None


class PalaceGraphSnapshot(BaseModel):
    available: bool
    palace_path: str
    stats: dict[str, Any] | None = None
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    capped: bool = False
    total_rooms: int | None = None
    reason: str | None = None


# ─── KG (T1+T2+T3 integration) ──────────────────────────────────────────


class KgTripleAddRequest(BaseModel):
    user_id: str = Field(description="Agent runner user id; routes to NATS subject")
    subject: str
    predicate: str = Field(description="Must be in canonical whitelist; see /api/kg/predicates")
    object: str
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    wait_visible_seconds: float = Field(2.0, ge=0.0, le=10.0)


class KgInvalidateRequest(BaseModel):
    user_id: str
    subject: str
    predicate: str
    object: str
    ended: str | None = Field(None, description="ISO8601; default NOW")
    wait_visible_seconds: float = Field(2.0, ge=0.0, le=10.0)


class KgWriteResult(BaseModel):
    status: str = Field(description="applied | pending")
    request_id: str
    triple_id: str | None = None


class KgTripleOut(BaseModel):
    id: str | None = None
    subject: str
    predicate: str
    object: str
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float | None = None
    source_drawer_id: str | None = None
    adapter_name: str | None = None


class KgEntityResponse(BaseModel):
    entity: str
    as_of: str | None = None
    direction: str = "outgoing"
    triples: list[KgTripleOut]


class KgTimelineResponse(BaseModel):
    entity_name: str | None = None
    since: str | None = None
    until: str | None = None
    events: list[KgTripleOut]


class KgStats(BaseModel):
    entities: int = 0
    triples_total: int = 0
    triples_active: int = 0
    triples_invalidated: int = 0


class KgPredicates(BaseModel):
    predicates: list[str]
    sensitive: list[str]
    count: int


class RecallRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(5, ge=1, le=50)
    voice: bool = False
    include_kg: bool | None = None
    include_sensitive_kg: bool = False


class RecallResponse(BaseModel):
    context: str
    kg_triples: list[KgTripleOut]
    records: list[dict[str, Any]]


# ─── MCP introspection ─────────────────────────────────────────────────


class McpToolOut(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class McpToolsResponse(BaseModel):
    tools: list[McpToolOut]
    count: int


# ─── Users page (lifecycle) ────────────────────────────────────────────


class UserDetail(BaseModel):
    user_id: str
    port: int
    enabled: bool
    palace_path: str
    mcp_http_url: str
    agent_reachable: bool
    palace_initialized: bool
    managed_by_admin: bool = False
    pid: int | None = None
    log_path: str | None = None


class UsersListResponse(BaseModel):
    users: list[UserDetail]
    users_yaml: str


class UserCreateRequest(BaseModel):
    id: str
    port: int = Field(ge=1, le=65535)
    enabled: bool = True
    palace_path: str = ""
    init_palace: bool = True
    auto_start: bool = True


class UserMutateResponse(BaseModel):
    user: UserDetail
    message: str = ""
