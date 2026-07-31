"""Runtime route contract for per-realm memory workers."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

DEFAULT_MEMORY_MCP_HOST = "127.0.0.1"
DEFAULT_MEMORY_MCP_PATH = "/mcp"
DEFAULT_MEMORY_MCP_BASE_PORT = 10030
MEMORY_MCP_PORT_SPAN = 2000


@dataclass(frozen=True)
class MemoryRuntimeRoute:
    """Resolved read/control route for a single memory realm."""

    memory_realm_id: str
    mcp_host: str = DEFAULT_MEMORY_MCP_HOST
    mcp_port: int = DEFAULT_MEMORY_MCP_BASE_PORT
    mcp_path: str = DEFAULT_MEMORY_MCP_PATH

    @property
    def mcp_http_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}{self.mcp_path}"


def stable_memory_realm_port(
    memory_realm_id: str,
    *,
    base_port: int = DEFAULT_MEMORY_MCP_BASE_PORT,
    used_ports: set[int] | None = None,
) -> int:
    """Allocate a deterministic MCP port without storing route data in the realm."""
    used = used_ports if used_ports is not None else set()
    base = min(max(base_port, 1), 65535)
    span = min(MEMORY_MCP_PORT_SPAN, 65535 - base + 1)
    seed = int.from_bytes(sha256(memory_realm_id.encode("utf-8")).digest()[:8], "big")
    for offset in range(span):
        port = base + ((seed + offset) % span)
        if port not in used:
            return port
    for port in range(1, 65536):
        if port not in used:
            return port
    raise ValueError("no free MCP port available for memory realm")


def memory_runtime_route_for_realm(
    memory_realm_id: str,
    *,
    base_port: int = DEFAULT_MEMORY_MCP_BASE_PORT,
    used_ports: set[int] | None = None,
    mcp_host: str = DEFAULT_MEMORY_MCP_HOST,
    mcp_path: str = DEFAULT_MEMORY_MCP_PATH,
) -> MemoryRuntimeRoute:
    """Resolve the runtime route for a realm from deployment defaults."""
    allocated_ports = used_ports if used_ports is not None else set()
    port = stable_memory_realm_port(
        memory_realm_id,
        base_port=base_port,
        used_ports=allocated_ports,
    )
    allocated_ports.add(port)
    return MemoryRuntimeRoute(
        memory_realm_id=memory_realm_id,
        mcp_host=mcp_host,
        mcp_port=port,
        mcp_path=mcp_path,
    )
