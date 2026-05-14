"""OpenAPI schemas for admin HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MemoryCreateRequest(BaseModel):
    wing: str = Field(description="Semantic wing id (matches NATS MEMORY_STORE / ingest wing).")
    room: str = Field(description="Room id within the wing.")
    text: str
    metadata: dict[str, Any] | None = None


class MemorySearchResponse(BaseModel):
    records: list[dict[str, Any]]


class MemoryListResponse(BaseModel):
    records: list[dict[str, Any]]
    total_hint: int | None = Field(
        default=None,
        description="Row count for this page; full collection size may be larger when paginating.",
    )


class HealthResponse(BaseModel):
    ok: bool = True
    palace_path: str
    steward_mode: str


class MemPalaceLayerInfo(BaseModel):
    """One level in the palace hierarchy (conceptual tier)."""

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
