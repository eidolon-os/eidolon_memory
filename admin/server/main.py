"""Eidolon memory admin HTTP API."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.health import router as health_router
from routers.hierarchy import router as hierarchy_router
from routers.memories import router as memories_router

from eidolon.memory.infrastructure.admin_mcp_client import eidolon_memory_mcp_stdio_session


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Keep one MCP stdio subprocess for the lifetime of the Admin API (matches IDE integration)."""
    async with eidolon_memory_mcp_stdio_session() as session:
        app.state.mcp_session = session
        yield


app = FastAPI(
    title="Eidolon Memory Admin",
    version="0.1.0",
    description=(
        "Read memories via MCP subprocess tools; enqueue writes on JetStream for the "
        "memory worker. Set EIDOLON_MEMORY_ADMIN_TOKEN to require Bearer auth."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5280",
        "http://localhost:5280",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")
api.include_router(health_router)
api.include_router(memories_router)
api.include_router(hierarchy_router)
app.include_router(api)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "eidolon-memory-admin", "docs": "/docs"}
