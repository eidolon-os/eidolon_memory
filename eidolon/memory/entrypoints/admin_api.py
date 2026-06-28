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
    GET    /api/admin/realms                 list all (with health)
    GET    /api/admin/realms/{memory_realm_id}
                                                single detail
    POST   /api/admin/reconcile              re-read admin registry
    POST   /api/admin/realms/{memory_realm_id}/memory/rebuild-index
                                                async rebuild vector index
    GET    /api/admin/memory/rebuild-index/{job_id}
                                                rebuild job status

All admin endpoints are namespaced under ``/api/admin`` and bound to
``settings.supervisor_http.host:port`` (config addition in this phase).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from eidolon.memory.application.user_admin import (
    UserAdmin,
    UserAdminError,
)
from eidolon.memory.config.palace_directory import validate_memory_space_id
from eidolon.memory.config.users import ConsolidatorUserConfig
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
    port: int | None = Field(None, ge=1, le=65535)
    enabled: bool = False
    palace_path: str = ""
    consolidator: _ConsolidatorIn | None = None

    @field_validator("user_id")
    @classmethod
    def _check_user_id(cls, v: str) -> str:
        # Reuse memory's existing canonical user-id validator so this stays
        # in sync with what supervisor / palace_directory accept.
        return validate_memory_space_id(v)


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

    @app.get("/api/admin/realms")
    async def list_realms(_request: Request) -> dict:
        users = user_admin.list_users()
        return {"realms": users, "memory_available": True}

    @app.get("/api/admin/realms/{memory_realm_id}")
    async def get_realm(memory_realm_id: str, _request: Request) -> dict:
        try:
            return user_admin.get_user(memory_realm_id)
        except UserAdminError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/admin/realms", status_code=201)
    async def create_user(body: CreateUserRequest) -> dict:
        del body
        raise HTTPException(
            status_code=409,
            detail=(
                "memory realm registry is read-only; create realms through "
                "eidolon_admin owner workspace APIs"
            ),
        )

    @app.post("/api/admin/reconcile")
    async def reconcile() -> dict:
        try:
            await user_admin.reconcile()
            return {"ok": True}
        except UserAdminError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/admin/realms/{memory_realm_id}/memory/rebuild-index", status_code=202)
    async def rebuild_memory_index(memory_realm_id: str) -> dict:
        try:
            return await user_admin.start_rebuild_index(memory_realm_id)
        except UserAdminError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/api/admin/memory/rebuild-index/{job_id}")
    async def get_rebuild_memory_index_job(job_id: str) -> dict:
        try:
            return user_admin.get_rebuild_index_job(job_id)
        except UserAdminError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/api/admin/realms/{memory_realm_id}/memory/rebuild-index")
    async def list_realm_rebuild_memory_index_jobs(memory_realm_id: str) -> dict:
        return {
            "jobs": user_admin.list_rebuild_index_jobs(
                memory_realm_id=memory_realm_id
            )
        }

    @app.get("/api/admin/health")
    async def health() -> dict:
        """Liveness for the admin HTTP layer itself (not user-worker health)."""
        return {"ok": True}

    return app
