"""Memory admin HTTP API — runs inside the supervisor process.

Why embedded in supervisor (not a separate process):
    The supervisor IS the authority over user lifecycle. Making the
    HTTP control surface a sibling process and forcing it to signal
    the supervisor via SIGHUP would (a) add cross-process coupling
    and (b) lose the ability to await reconcile in-process. Embedding
    keeps the control path one in-process method call away from the
    state machine that actually runs.

The existing read-only ``discovery_server.py`` stays as a separate
process for now — admin and agent talk to discovery for routing
reads. Write operations on users go through this HTTP surface.

Routes:
    GET    /api/admin/users                  list all (with health)
    GET    /api/admin/users/{user_id}        single detail
    POST   /api/admin/users                  create + wait-for-worker
    DELETE /api/admin/users/{user_id}        cascade-delete with compensation

All admin endpoints are namespaced under ``/api/admin`` and bound to
``settings.supervisor_http.host:port`` (config addition in this phase).
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from eidolon.memory.application.user_admin import (
    UserAdmin,
    UserAdminError,
)
from eidolon.memory.config.users import ConsolidatorUserConfig, validate_user_id
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


# ---- request models (HTTP wire shapes) -------------------------------------


class _ConsolidatorIn(BaseModel):
    enabled: bool = False
    interval_hours: float = Field(6.0, gt=0)
    window_days: int = Field(30, gt=0)
    min_drawers: int = Field(3, ge=1)
    min_confidence: float = Field(0.6, ge=0.0, le=1.0)

    def to_domain(self) -> ConsolidatorUserConfig:
        return ConsolidatorUserConfig(
            enabled=self.enabled,
            interval_hours=self.interval_hours,
            window_days=self.window_days,
            min_drawers=self.min_drawers,
            min_confidence=self.min_confidence,
        )


class CreateUserRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    # Optional explicit port; if None, user_admin auto-allocates from a range.
    port: Optional[int] = Field(None, ge=1, le=65535)
    palace_path: str = ""
    consolidator: Optional[_ConsolidatorIn] = None

    @field_validator("user_id")
    @classmethod
    def _check_user_id(cls, v: str) -> str:
        # Reuse memory's existing canonical user-id validator so this stays
        # in sync with what supervisor / palace_directory accept.
        return validate_user_id(v)


# ---- factory --------------------------------------------------------------


def build_admin_api(user_admin: UserAdmin) -> FastAPI:
    """Build the FastAPI app, with ``user_admin`` captured in closures.

    The supervisor's main() builds one ``UserAdmin`` instance bound to its
    own Supervisor, then passes it here. Tests build their own with a
    stub supervisor.
    """
    app = FastAPI(
        title="Eidolon Memory Admin",
        # No /docs in production — the API surface is small and admin-internal,
        # docs would just be noise. Tests can still hit endpoints directly.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/api/admin/users")
    async def list_users(_request: Request) -> dict:
        users = user_admin.list_users()
        return {"users": users, "memory_available": True}

    @app.get("/api/admin/users/{user_id}")
    async def get_user(user_id: str, _request: Request) -> dict:
        try:
            return user_admin.get_user(user_id)
        except UserAdminError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/admin/users", status_code=201)
    async def create_user(body: CreateUserRequest) -> dict:
        try:
            return await user_admin.create_user(
                user_id=body.user_id,
                port=body.port,
                palace_path=body.palace_path,
                consolidator=body.consolidator.to_domain() if body.consolidator else None,
            )
        except UserAdminError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.delete("/api/admin/users/{user_id}")
    async def delete_user(user_id: str) -> dict:
        try:
            return await user_admin.delete_user(user_id)
        except UserAdminError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/api/admin/health")
    async def health() -> dict:
        """Liveness for the admin HTTP layer itself (not user-worker health)."""
        return {"ok": True}

    return app
