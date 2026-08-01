"""Behaviour of the two capabilities the logic layer asks a store for.

Warming and room enumeration are optional: a store either offers them or does
not, and the logic layer must work either way. That branch is the whole point of
making them capabilities rather than backend-name checks, so it is what these
tests exercise.
"""

from __future__ import annotations

from typing import Any

import pytest

from eidolon.memory.application.palace_graph import build_palace_graph
from eidolon.memory.application.runtime_warm import _wings_worth_warming, warm_read_path
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import RoomGraphBackend, WarmableBackend
from eidolon.memory.domain.room_graph import RoomGraphSnapshot, RoomNode


class _PlainStore:
    """A store offering neither capability — a remote one, in practice."""


class _WarmableStore:
    def __init__(self) -> None:
        self.warmed_wings: list[str] | None = None

    async def warm_read_path(self, *, wings) -> None:
        self.warmed_wings = list(wings)


class _RoomStore:
    def __init__(self, snapshot: RoomGraphSnapshot | None) -> None:
        self._snapshot = snapshot

    async def room_graph(self) -> RoomGraphSnapshot | None:
        return self._snapshot


def _settings(**recall: Any) -> MemorySettings:
    return MemorySettings.model_validate({"recall": recall} if recall else {})


# ── warming ──────────────────────────────────────────────────────────────────


def test_the_capabilities_are_recognised_structurally() -> None:
    """These are the checks the logic layer makes, so they have to hold for a
    store that never imported the protocol."""

    assert isinstance(_WarmableStore(), WarmableBackend)
    assert isinstance(_RoomStore(None), RoomGraphBackend)
    assert not isinstance(_PlainStore(), WarmableBackend)
    assert not isinstance(_PlainStore(), RoomGraphBackend)


async def test_a_store_without_warming_is_not_an_error() -> None:
    """With a vector server there is nothing on this side to warm.

    Returning quietly rather than raising or logging a failure is the difference
    between "this deployment does not need it" and "startup went wrong".
    """

    await warm_read_path(_PlainStore(), _settings())


async def test_warming_asks_for_the_voice_wings() -> None:
    """Voice has the tightest budget, so its wings are the ones whose first read
    must not be the slow one."""

    store = _WarmableStore()

    await warm_read_path(store, _settings(voice_wings=["Wing_Life", "Wing_Work"]))

    assert store.warmed_wings == ["Wing_Life", "Wing_Work"]


def test_without_voice_wings_everything_but_privacy_is_warmed() -> None:
    """Warming a private wing would pull it into caches for a path that never
    reads it."""

    wings = _wings_worth_warming(_settings())

    assert wings
    assert "Wing_Privacy" not in wings


# ── the room graph ───────────────────────────────────────────────────────────


async def test_a_store_that_cannot_enumerate_rooms_reports_unavailable() -> None:
    """The caller is a visualisation. An error there would read as a broken
    service rather than a feature this storage does not have."""

    result = await build_palace_graph(_PlainStore(), max_nodes=10, max_edges=10)

    assert result["available"] is False
    assert "cannot enumerate" in result["reason"]
    assert result["nodes"] == []


async def test_a_palace_with_no_collection_reports_unavailable() -> None:
    result = await build_palace_graph(_RoomStore(None), max_nodes=10, max_edges=10)

    assert result["available"] is False
    assert result["reason"] == "palace collection missing"


async def test_an_empty_palace_is_available_and_empty() -> None:
    """Distinct from unavailable: there is a graph, it just has nothing in it."""

    store = _RoomStore(RoomGraphSnapshot(rooms={}, stats={"rooms": 0}))

    result = await build_palace_graph(store, max_nodes=10, max_edges=10)

    assert result["available"] is True
    assert result["total_rooms"] == 0
    assert result["stats"] == {"rooms": 0}


async def test_rooms_shared_between_wings_become_edges() -> None:
    store = _RoomStore(
        RoomGraphSnapshot(
            rooms={
                "tea": RoomNode(wings=("Wing_Life", "Wing_Work"), count=5),
                "code": RoomNode(wings=("Wing_Work",), count=3),
            }
        )
    )

    result = await build_palace_graph(store, max_nodes=10, max_edges=10)

    assert result["total_rooms"] == 2
    assert [edge["source"] for edge in result["edges"]] == ["tea"]
    assert result["edges"][0]["target"] == "code"
    assert result["edges"][0]["shared_wings"] == ["Wing_Work"]


async def test_a_room_under_two_wings_is_marked_as_a_tunnel() -> None:
    store = _RoomStore(
        RoomGraphSnapshot(
            rooms={
                "tea": RoomNode(wings=("Wing_Life", "Wing_Work"), count=1),
                "code": RoomNode(wings=("Wing_Work",), count=99),
            }
        )
    )

    result = await build_palace_graph(store, max_nodes=10, max_edges=10)

    by_id = {node["id"]: node for node in result["nodes"]}
    assert by_id["tea"]["is_tunnel"] is True
    assert by_id["code"]["is_tunnel"] is False


async def test_tunnels_outrank_busier_rooms_when_nodes_are_capped() -> None:
    """A cap that dropped tunnels would remove the only edges the graph can draw,
    leaving a picture of unconnected dots."""

    store = _RoomStore(
        RoomGraphSnapshot(
            rooms={
                "busy": RoomNode(wings=("Wing_Work",), count=1000),
                "tunnel": RoomNode(wings=("Wing_Life", "Wing_Work"), count=1),
            }
        )
    )

    result = await build_palace_graph(store, max_nodes=1, max_edges=10)

    assert [node["id"] for node in result["nodes"]] == ["tunnel"]
    assert result["capped"] is True


async def test_the_edge_cap_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    rooms = {
        f"room{index}": RoomNode(wings=("Wing_Work", "Wing_Life"), count=index)
        for index in range(6)
    }
    store = _RoomStore(RoomGraphSnapshot(rooms=rooms))

    result = await build_palace_graph(store, max_nodes=6, max_edges=3)

    assert len(result["edges"]) == 3
    assert result["capped"] is True
