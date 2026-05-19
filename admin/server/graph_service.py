"""Graph snapshots via per-user agent_runner MCP (D1 contract).

The admin process MUST NOT open chromadb or knowledge_graph.sqlite3 directly —
agent_runner is the single owner of those files. We call the MCP tools
``eidolon_memory_palace_graph`` and ``eidolon_memory_kg_snapshot`` instead,
which run inside agent_runner under ``LockedBackend.lock`` /
``LockedKnowledgeGraph``.
"""

from __future__ import annotations

from typing import Any

from mcp.client.session import ClientSession

from mcp_client import call_tool_json


async def knowledge_graph_snapshot(
    mcp: ClientSession,
    *,
    palace_path: str,
    max_triples: int = 400,
    current_only: bool = True,
    entity: str | None = None,
    include_sensitive: bool = False,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "available": False,
        "palace_path": palace_path,
        "kg_path": "",
        "stats": None,
        "nodes": [],
        "edges": [],
        "capped": False,
        "triple_count": None,
        "reason": None,
    }
    args: dict[str, Any] = {
        "max_triples": max_triples,
        "current_only": current_only,
        "include_sensitive": include_sensitive,
    }
    if entity and entity.strip():
        args["entity"] = entity.strip()
    try:
        payload = await call_tool_json(mcp, "eidolon_memory_kg_snapshot", args)
    except Exception as exc:
        base["reason"] = str(exc)
        return base
    if not isinstance(payload, dict):
        base["reason"] = "unexpected MCP kg_snapshot payload"
        return base

    triples = payload.get("triples") or []
    stats = payload.get("stats")
    nodes, edges, node_capped = _triples_to_graph(triples, max_nodes=max_triples)
    base.update(
        {
            "available": True,
            "stats": stats,
            "nodes": nodes,
            "edges": edges,
            "capped": bool(payload.get("capped")) or node_capped,
            "triple_count": int(payload.get("triple_count") or len(triples)),
        }
    )
    return base


async def palace_graph_snapshot(
    mcp: ClientSession,
    *,
    palace_path: str,
    max_nodes: int = 120,
    max_edges: int = 200,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "available": False,
        "palace_path": palace_path,
        "stats": None,
        "nodes": [],
        "edges": [],
        "capped": False,
        "total_rooms": None,
        "reason": None,
    }
    try:
        payload = await call_tool_json(
            mcp,
            "eidolon_memory_palace_graph",
            {"max_nodes": max_nodes, "max_edges": max_edges},
        )
    except Exception as exc:
        base["reason"] = str(exc)
        return base
    if not isinstance(payload, dict):
        base["reason"] = "unexpected MCP palace_graph payload"
        return base
    base.update(
        {
            "available": bool(payload.get("available")),
            "stats": payload.get("stats"),
            "nodes": payload.get("nodes") or [],
            "edges": payload.get("edges") or [],
            "capped": bool(payload.get("capped")),
            "total_rooms": payload.get("total_rooms"),
            "reason": payload.get("reason"),
        }
    )
    return base


def _triples_to_graph(
    triples: list[dict[str, Any]],
    *,
    max_nodes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Render KG triples as node/edge objects for the admin viz."""
    node_map: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def ensure_node(name: str) -> str | None:
        nid = (name or "").strip() or "?"
        if nid not in node_map:
            if len(node_map) >= max_nodes:
                return None
            node_map[nid] = {
                "id": nid,
                "label": nid,
                "kind": "entity",
                "entity_type": _guess_entity_type(nid),
            }
        return nid

    capped = False
    for idx, row in enumerate(triples):
        sub = str(row.get("subject") or "")
        obj = str(row.get("object") or "")
        pred = str(row.get("predicate") or "")
        sid = ensure_node(sub)
        oid = ensure_node(obj)
        if sid is None or oid is None:
            capped = True
            continue
        edges.append(
            {
                "id": row.get("id") or f"t{idx}",
                "source": sid,
                "target": oid,
                "label": pred,
                "valid_from": row.get("valid_from"),
                "valid_to": row.get("valid_to"),
                "current": row.get("valid_to") is None,
            }
        )

    return list(node_map.values()), edges, capped


def _guess_entity_type(name: str) -> str:
    if ":" in name:
        return name.split(":", 1)[0]
    return "entity"
