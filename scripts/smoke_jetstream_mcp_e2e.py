#!/usr/bin/env python3
"""End-to-end JetStream + MCP smoke test — pending D1 stage-7 rewrite.

Pre-D1 used the standalone ``eidolon-memory-mcp`` HTTP server + ``mcp_http_client``;
in D1 the MCP server lives inside agent_runner. Will be rewritten to start an
agent_runner subprocess, publish a turn via ``JetStreamTurnPublisher`` to
``agent.memory.conversation.turn.<user_id>``, then call the runner's
control-plane MCP via ``mcp`` SDK and assert recall.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "[smoke_jetstream_mcp_e2e] disabled until D1 stage 7. "
        "See docs/architecture-d1-readwrite-split.md §10 (V5)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
