"""Pick the part of a palace's room graph that is worth looking at.

A palace can hold thousands of rooms, which no client can usefully render, so
this ranks them and caps both nodes and edges. Rooms that appear under more than
one wing rank first — they are the tunnels between wings, and the reason to draw
this at all.

Reading the rooms is the store's job (:class:`RoomGraphBackend`); everything here
is presentation, which is why it can be changed without touching storage.
"""

from __future__ import annotations

from typing import Any

from eidolon.memory.domain.ports import RoomGraphBackend
from eidolon.memory.domain.room_graph import RoomGraphSnapshot


async def build_palace_graph(
    backend: Any,
    *,
    max_nodes: int,
    max_edges: int,
) -> dict[str, Any]:
    """The room graph as a client can consume it.

    Never raises for an absent graph: a store that cannot enumerate rooms, and a
    palace that has none yet, both come back as ``available`` with a reason. The
    caller is a visualisation, and an error there would read as a broken service
    rather than an empty one.
    """

    if not isinstance(backend, RoomGraphBackend):
        return _unavailable("this store cannot enumerate rooms")

    snapshot = await backend.room_graph()
    if snapshot is None:
        return _unavailable("palace collection missing")

    return _render(snapshot, max_nodes=max_nodes, max_edges=max_edges)


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "stats": None,
        "nodes": [],
        "edges": [],
        "capped": False,
        "total_rooms": 0,
    }


def _render(
    snapshot: RoomGraphSnapshot,
    *,
    max_nodes: int,
    max_edges: int,
) -> dict[str, Any]:
    rooms = snapshot.rooms
    if not rooms:
        return {
            "available": True,
            "reason": "palace graph is empty",
            "stats": snapshot.stats,
            "nodes": [],
            "edges": [],
            "capped": False,
            "total_rooms": 0,
        }

    # Tunnels first, then the busiest rooms: a cap that dropped tunnels would
    # remove the only edges the graph can draw.
    ranked = sorted(
        rooms.items(),
        key=lambda item: (item[1].is_tunnel, item[1].count),
        reverse=True,
    )
    picked = ranked[:max_nodes]

    nodes = [
        {
            "id": room,
            "label": room,
            "kind": "room",
            "wings": list(node.wings),
            "halls": list(node.halls),
            "count": node.count,
            "is_tunnel": node.is_tunnel,
        }
        for room, node in picked
    ]

    edges = _edges_between([room for room, _ in picked], rooms, max_edges=max_edges)

    return {
        "available": True,
        "reason": None,
        "stats": snapshot.stats,
        "nodes": nodes,
        "edges": edges,
        "capped": len(ranked) > len(picked) or len(edges) >= max_edges,
        "total_rooms": len(rooms),
    }


def _edges_between(
    room_ids: list[str],
    rooms: dict[str, RoomNode],
    *,
    max_edges: int,
) -> list[dict[str, Any]]:
    """Join two rooms when they share a wing, up to the cap."""

    edges: list[dict[str, Any]] = []
    for index, left in enumerate(room_ids):
        left_wings = set(rooms[left].wings)
        for right in room_ids[index + 1 :]:
            shared = sorted(left_wings & set(rooms[right].wings))
            if not shared:
                continue
            edges.append(
                {
                    "id": f"{left}--{right}",
                    "source": left,
                    "target": right,
                    "label": shared[0] if len(shared) == 1 else f"{len(shared)} wings",
                    "shared_wings": shared,
                }
            )
            if len(edges) >= max_edges:
                return edges
    return edges
