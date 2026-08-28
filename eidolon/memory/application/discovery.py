"""Agent routing discovery contract for eidolon-agent.

Discovery is a runtime contract for agents: which MCP endpoint serves each
memory realm. It does not derive routing from tenant/user/companion triples.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from eidolon_memory_contracts import (
    MEMORY_COMMAND_BASE,
    MEMORY_CONVERSATION_TURN_BASE,
    validate_memory_space_id,
)
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.registry import load_users_config
from eidolon.memory.config.users import UserEntry
from eidolon.memory.entrypoints.recollections_http import RECOLLECTIONS_PATH

DISCOVERY_VERSION = 2


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


def discovery_memory_realms(settings: MemorySettings) -> list[UserEntry]:
    """Return memory realms exposed to agents, with a dev fallback."""
    enabled = load_users_config(settings).enabled_users()
    if enabled:
        return enabled
    return [
        UserEntry(
            id="default",
            port=settings.mcp_http.port,
            enabled=True,
        )
    ]


def recollections_url(settings: MemorySettings, *, port: int) -> str:
    """The plain-HTTP read surface on a runner, beside its MCP transport."""

    base = settings.mcp_http.base_url(port=port)
    root = base[: -len(settings.mcp_http.path)] if base.endswith(settings.mcp_http.path) else base
    return f"{root.rstrip('/')}{RECOLLECTIONS_PATH}"


async def build_agent_routing_discovery(settings: MemorySettings) -> dict[str, Any]:
    """Build the stable discovery response consumed by eidolon-agent."""
    realms = discovery_memory_realms(settings)
    reachability = await asyncio.gather(
        *[
            probe_mcp_http(settings.mcp_http.base_url(port=realm.port))
            for realm in realms
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
        "memory_realms": [
            {
                "memory_space_id": validate_memory_space_id(realm.id),
                "memory_realm_id": validate_memory_space_id(realm.id),
                "owner_id": realm.owner_id,
                "enabled": realm.enabled,
                "mcp_http_url": settings.mcp_http.base_url(port=realm.port),
                "ops_mcp_http_url": settings.mcp_http.ops_base_url(port=realm.port),
                # Where a person's own Host reads this space from. Published
                # rather than derived: a consumer that had to cut the MCP path
                # off the URL above would be guessing at this one, and would
                # keep guessing correctly right up until either path moved.
                "recollections_url": recollections_url(settings, port=realm.port),
                "mcp_auth": {"type": "none"},
                "agent_reachable": reachable,
            }
            for realm, reachable in zip(realms, reachability, strict=True)
        ],
    }
