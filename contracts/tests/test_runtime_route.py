from __future__ import annotations

from eidolon_memory_contracts import (
    MemoryRuntimeRoute,
    memory_runtime_route_for_realm,
    stable_memory_realm_port,
)


def test_memory_runtime_route_derives_stable_mcp_url() -> None:
    first = memory_runtime_route_for_realm("r:benchmark:default", base_port=10030)
    second = memory_runtime_route_for_realm("r:benchmark:default", base_port=10030)

    assert first == second
    assert first.mcp_http_url == f"http://127.0.0.1:{first.mcp_port}/mcp"
    assert first.ops_mcp_http_url == f"http://127.0.0.1:{first.mcp_port}/ops/mcp"
    assert 10030 <= first.mcp_port <= 12029


def test_stable_memory_realm_port_avoids_used_ports() -> None:
    first = stable_memory_realm_port("r:benchmark:default", base_port=10030)
    second = stable_memory_realm_port(
        "r:benchmark:default",
        base_port=10030,
        used_ports={first},
    )

    assert second != first
    assert 10030 <= second <= 12029


def test_memory_runtime_route_supports_explicit_host_and_path() -> None:
    route = memory_runtime_route_for_realm(
        "r:benchmark:default",
        base_port=10030,
        mcp_host="127.0.0.2",
        mcp_path="/custom-mcp",
        ops_mcp_path="/custom-ops-mcp",
    )

    assert isinstance(route, MemoryRuntimeRoute)
    assert route.mcp_http_url == f"http://127.0.0.2:{route.mcp_port}/custom-mcp"
    assert route.ops_mcp_http_url == (
        f"http://127.0.0.2:{route.mcp_port}/custom-ops-mcp"
    )
