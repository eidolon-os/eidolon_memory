"""Eidolon memory admin HTTP API (D1: per-user agent_runner control-plane)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp_sessions import UserMcpSessionManager
from routers.graph import router as graph_router
from routers.health import router as health_router
from routers.hierarchy import router as hierarchy_router
from routers.kg import recall_router, router as kg_router
from routers.memories import router as memories_router

from eidolon.memory.config.memory_settings import get_memory_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_memory_settings()
    manager = UserMcpSessionManager(settings)
    await manager.open()
    app.state.mcp_manager = manager
    try:
        yield
    finally:
        await manager.close()


app = FastAPI(
    title="Eidolon Memory Admin",
    version="0.2.0",
    description=(
        "D1: reads call each user's agent_runner MCP (users.yaml port); writes publish "
        "ConversationTurnPayload to agent.memory.conversation.turn.<user_id>. "
        "Start eidolon-memory-supervisor or eidolon-memory-agent before Admin."
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
api.include_router(graph_router)
api.include_router(kg_router)
api.include_router(recall_router)
app.include_router(api)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "eidolon-memory-admin", "docs": "/docs"}
