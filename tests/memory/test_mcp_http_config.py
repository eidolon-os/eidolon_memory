"""MCP Streamable HTTP settings and tool wiring."""

from __future__ import annotations

import pytest
import yaml

from eidolon.memory.config.memory_settings import McpHttpConfig, load_memory_settings
from eidolon.memory.entrypoints import mcp_server


def test_mcp_http_base_url_defaults():
    cfg = McpHttpConfig()
    assert cfg.base_url() == "http://127.0.0.1:8030/mcp"


def test_mcp_http_base_url_from_yaml(tmp_path):
    data = {
        "wings": [{"id": "Wing_Life", "display_name": "life"}],
        "mcp_http": {"host": "0.0.0.0", "port": 9001, "path": "/memory/mcp"},
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings = load_memory_settings(p)
    assert settings.mcp_http.base_url() == "http://0.0.0.0:9001/memory/mcp"


def test_build_server_registers_tools(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    mcp_server.reset_backend_cache()
    server = mcp_server.build_server()
    tool_names = {t.name for t in server._tool_manager.list_tools()}
    assert "eidolon_memory_search" in tool_names
    assert "eidolon_memory_list" in tool_names
    assert "eidolon_memory_hierarchy_snapshot" in tool_names
