"""Agent routing discovery contract for eidolon-agent.

Discovery is a runtime contract for agents (where do I send turns, which MCP
port serves which user) — not an operations UI surface. Lives in core so
the contract is owned by the memory package itself.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from eidolon_sdk.memory import (
    MEMORY_COMMAND_BASE,
    MEMORY_CONVERSATION_TURN_BASE,
    validate_memory_space_id,
)
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.users import UserEntry, load_users_config

DISCOVERY_VERSION = 1


def _memory_space_parts(memory_space_id: str) -> tuple[str, str, str]:
    """Return tenant, owner user, and companion ids from a memory-space id."""

    validated = validate_memory_space_id(memory_space_id)
    tenant_id, owner_user_id, companion_id = validated.split(".", 2)
    return tenant_id, owner_user_id, companion_id


async def probe_mcp_http(url: str, *, timeout_seconds: float = 1.5) -> bool:
    """Return whether a Streamable HTTP MCP endpoint accepts initialization."""
    try:
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=False,
            ) as client:
                async with streamable_http_client(url, http_client=client) as (
                    read,
                    write,
                    _get_session_id,
                ):
                    del _get_session_id
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return True
    except Exception:
        return False


def discovery_users(settings: MemorySettings) -> list[UserEntry]:
    """Return users exposed to agents, with the legacy single-user fallback."""
    enabled = load_users_config(settings).enabled_users()
    if enabled:
        return enabled
    # Legacy single-user fallback. ``UserEntry.id`` is a memory_space_id
    # (<tenant>.<owner_user>.<persona>), so the default space is fully qualified.
    return [
        UserEntry(
            id="default.default.default",
            port=settings.mcp_http.port,
            enabled=True,
        )
    ]


async def build_agent_routing_discovery(settings: MemorySettings) -> dict[str, Any]:
    """Build the stable discovery response consumed by eidolon-agent."""
    users = discovery_users(settings)
    reachability = await asyncio.gather(
        *[
            probe_mcp_http(settings.mcp_http.base_url(port=user.port))
            for user in users
        ]
    )
    return {
        "version": DISCOVERY_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "nats": {
            "url": settings.nats.url,
            "stream": settings.nats.stream,
            "turn_subject_template": (
                f"{MEMORY_CONVERSATION_TURN_BASE}.{{memory_space_token}}"
            ),
            "cmd_subject_template": (
                f"{MEMORY_COMMAND_BASE}.{{memory_space_token}}"
            ),
        },
        "users": [
            {
                "memory_space_id": user.id,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
                "companion_id": companion_id,
                "persona_id": companion_id,
                "enabled": user.enabled,
                "mcp_http_url": settings.mcp_http.base_url(port=user.port),
                "mcp_auth": {"type": "none"},
                "agent_reachable": reachable,
            }
            for user, reachable in zip(users, reachability, strict=True)
            for tenant_id, owner_user_id, companion_id in [_memory_space_parts(user.id)]
        ],
    }
