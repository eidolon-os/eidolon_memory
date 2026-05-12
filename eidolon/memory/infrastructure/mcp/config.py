"""How to spawn or attach to the MemPalace MCP server (stdio)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class McpServerLaunchConfig:
    """Stdio MCP server parameters (passed to ``mcp.client.stdio``)."""

    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None

    @classmethod
    def from_environ(cls) -> McpServerLaunchConfig:
        cmd = os.environ.get("EIDOLON_MEMORY_MCP_COMMAND", "").strip()
        raw_args = os.environ.get("EIDOLON_MEMORY_MCP_ARGS", "")
        args = [a for a in raw_args.split() if a] if raw_args else []
        cwd = os.environ.get("EIDOLON_MEMORY_MCP_CWD") or None
        return cls(command=cmd, args=args, cwd=cwd)

    def is_configured(self) -> bool:
        return bool(self.command)
