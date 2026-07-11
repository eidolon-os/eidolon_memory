"""Control-plane MCP factory + McpHttpConfig URL helpers (D1)."""

from __future__ import annotations

import yaml

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.config.memory_settings import (
    McpHttpConfig,
    load_memory_settings,
)
from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp


def test_mcp_http_base_url_defaults() -> None:
    cfg = McpHttpConfig()
    assert cfg.base_url() == "http://127.0.0.1:10030/mcp"
    assert cfg.stateless_http is True
    assert cfg.json_response is True


def test_mcp_http_base_url_port_override() -> None:
    cfg = McpHttpConfig()
    assert cfg.base_url(port=9999) == "http://127.0.0.1:9999/mcp"


def test_mcp_http_base_url_from_yaml(tmp_path):
    data = {
        "mcp_http": {"host": "0.0.0.0", "port": 9001, "path": "/memory/mcp"},
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings = load_memory_settings(p)
    assert settings.mcp_http.base_url() == "http://0.0.0.0:9001/memory/mcp"


def test_build_control_plane_mcp_registers_tools(tmp_path) -> None:
    settings = load_memory_settings()
    backend = FakeMemoryBackend()
    mcp = build_control_plane_mcp(
        backend,
        settings,
        memory_space_id="default.alice.default",
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9999,
    )
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "eidolon_memory_search" in tool_names
    assert "eidolon_memory_recall_context" in tool_names
    assert "eidolon_memory_status" in tool_names
    assert "eidolon_memory_list" in tool_names
    assert "eidolon_memory_hierarchy_snapshot" in tool_names
    # D1: write tools are intentionally absent (writes go via NATS only)
    assert "eidolon_memory_delete" not in tool_names
