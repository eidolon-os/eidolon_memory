"""Build the cross-wing tunnel-room graph for visualization.

Pure application-layer logic: takes a LockedBackend (for the shared lock) +
palace path, returns a JSON-shaped dict consumed by MCP `palace_graph` tool
and Admin / IDE clients.

Lives here rather than in the MCP tool body so the entrypoints layer stays
a thin shell over the application layer.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def build_palace_graph(
    backend: Any,
    *,
    palace_path: str,
    max_nodes: int,
    max_edges: int,
) -> dict[str, Any]:
    """Run ``mempalace.palace_graph.build_graph`` against the agent_runner's
    collection. Uses ``LockedBackend.lock`` to serialize with reads/writes —
    chroma's sqlite-backed cursor must not race the write path. Non-locked
    backends (tests) execute unlocked.
    """
    def _run() -> dict[str, Any]:
        from mempalace.palace import get_collection
        from mempalace.palace_graph import build_graph, graph_stats

        col = get_collection(palace_path, create=False)
        if col is None:
            return {
                "available": False,
                "reason": "palace collection missing",
                "stats": None,
                "nodes": [],
                "edges": [],
                "capped": False,
                "total_rooms": 0,
            }
        raw_nodes, _raw_edges = build_graph(col=col)
        stats = graph_stats(col=col)

        if not raw_nodes:
            return {
                "available": True,
                "reason": "palace graph is empty",
                "stats": stats,
                "nodes": [],
                "edges": [],
                "capped": False,
                "total_rooms": 0,
            }

        ranked = sorted(
            raw_nodes.items(),
            key=lambda item: (len(item[1]["wings"]) >= 2, item[1]["count"]),
            reverse=True,
        )
        picked = ranked[:max_nodes]
        node_ids = {room for room, _ in picked}

        nodes = [
            {
                "id": room,
                "label": room,
                "kind": "room",
                "wings": list(data["wings"]),
                "halls": list(data.get("halls") or []),
                "count": int(data.get("count") or 0),
                "is_tunnel": len(data.get("wings") or []) >= 2,
            }
            for room, data in picked
        ]

        edges: list[dict[str, Any]] = []
        rooms_list = list(node_ids)
        for i, ra in enumerate(rooms_list):
            wa = set(raw_nodes[ra]["wings"])
            for rb in rooms_list[i + 1 :]:
                wb = set(raw_nodes[rb]["wings"])
                shared = sorted(wa & wb)
                if not shared:
                    continue
                edges.append(
                    {
                        "id": f"{ra}--{rb}",
                        "source": ra,
                        "target": rb,
                        "label": shared[0] if len(shared) == 1 else f"{len(shared)} wings",
                        "shared_wings": shared,
                    }
                )
                if len(edges) >= max_edges:
                    break
            if len(edges) >= max_edges:
                break

        return {
            "available": True,
            "reason": None,
            "stats": stats,
            "nodes": nodes,
            "edges": edges,
            "capped": len(ranked) > len(picked) or len(edges) >= max_edges,
            "total_rooms": len(raw_nodes),
        }

    lock = getattr(backend, "lock", None)
    if lock is not None:
        async with lock:
            return await asyncio.to_thread(_run)
    return await asyncio.to_thread(_run)
