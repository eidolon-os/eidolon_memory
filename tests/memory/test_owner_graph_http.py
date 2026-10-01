"""The Owner graph is bounded and uses the same audience rule as recall."""

from __future__ import annotations

from typing import Any

import pytest
from eidolon_memory_contracts import OWNER_AUDIENCE, companion_audience
from eidolon_memory_contracts.owner import MemoryGraph
from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.domain.kg import KgTripleRecord
from eidolon.memory.domain.wings import CANONICAL_WINGS
from eidolon.memory.entrypoints.owner_memory_http import GRAPH_PATH, owner_memory_routes

TOKEN = "memory-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPACE = "realm_owner_one"


class _Graph:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def timeline(self, **kwargs: Any) -> list[KgTripleRecord]:
        self.calls.append(kwargs)
        return [
            KgTripleRecord(
                id="stmt-1",
                subject="我",
                predicate="likes",
                object="乌龙茶",
                confidence=0.94,
                recorded_at="2026-08-28T08:00:00Z",
            )
        ]


class _Runtime:
    def __init__(self, graph: _Graph) -> None:
        self.kg = graph


class _Service:
    def __init__(self, graph: _Graph) -> None:
        self.runtime = _Runtime(graph)
        self.contexts: list[Any] = []

    async def runtime_for(self, context: Any) -> _Runtime:
        self.contexts.append(context)
        return self.runtime


class _Settings:
    wings = CANONICAL_WINGS


def test_selected_companion_sees_owner_derived_and_its_private_graph() -> None:
    graph = _Graph()
    service = _Service(graph)
    app = Starlette(
        routes=owner_memory_routes(
            service=service,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
            memory_space_id=SPACE,
            owner_id="owner-1",
            service_token=TOKEN,
        )
    )

    with TestClient(app) as http:
        body = http.get(
            f"{GRAPH_PATH}?companion_id=c_mochi",
            headers=AUTH,
        ).json()

    assert graph.calls[0]["audiences"] == (
        OWNER_AUDIENCE,
        companion_audience("c_mochi"),
    )
    assert graph.calls[0]["current_only"] is True
    assert graph.calls[0]["include_sensitive"] is False
    assert body["nodes"] == [
        {"node_id": "乌龙茶", "label": "乌龙茶", "degree": 1},
        {"node_id": "我", "label": "我", "degree": 1},
    ]
    assert body["edges"][0]["predicate"] == "likes"


def test_no_companion_means_the_owners_whole_graph() -> None:
    """The Owner reading their own graph: every audience (``audiences=None``).

    It used to be the Owner layer only — empty, since ordinary turns are written
    to one Companion's audience.
    """
    graph = _Graph()
    service = _Service(graph)
    app = Starlette(
        routes=owner_memory_routes(
            service=service,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
            memory_space_id=SPACE,
            owner_id="owner-1",
            service_token=TOKEN,
        )
    )

    with TestClient(app) as http:
        response = http.get(GRAPH_PATH, headers=AUTH)

    assert response.status_code == 200
    assert graph.calls[0]["audiences"] is None
    MemoryGraph.model_validate(response.json())


@pytest.mark.asyncio
async def test_real_graph_pages_tied_dates_without_losing_scope_or_history(tmp_path, monkeypatch):
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
    from eidolon.memory.domain.space_lock import SpaceLock

    monkeypatch.setattr(
        "eidolon.memory.adapters.kg_sqlite.now_iso",
        lambda: "2026-08-28T08:00:00Z",
    )
    graph = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE, lock=SpaceLock())
    try:
        for i in range(125):
            await graph.add_triple(
                subject="我",
                predicate="likes",
                object=f"物品{i}",
                audience=OWNER_AUDIENCE,
                valid_from="2026-08-28T08:00:00Z",
            )
        await graph.invalidate(subject="我", predicate="likes", object="物品0", ended="2026-09-01")
        await graph.add_triple(
            subject="我",
            predicate="likes",
            object="另一个伙伴的秘密",
            audience=companion_audience("c_nori"),
        )
        await graph.add_triple(
            subject="我",
            predicate="has_health_condition",
            object="敏感资料",
            audience=OWNER_AUDIENCE,
        )
        app = Starlette(
            routes=owner_memory_routes(
                service=_Service(graph),
                settings=_Settings(),
                memory_space_id=SPACE,
                owner_id="owner-1",
                service_token=TOKEN,
            )
        )
        with TestClient(app) as http:
            seen = []
            cursor = None
            while True:
                params = {"companion_id": "c_mochi", "limit": 17}
                if cursor:
                    params["cursor"] = cursor
                response = http.get(GRAPH_PATH, params=params, headers=AUTH)
                assert response.status_code == 200, response.text
                page = MemoryGraph.model_validate(response.json())
                seen.extend(edge.edge_id for edge in page.edges)
                cursor = page.next_cursor
                if not cursor:
                    break
            assert len(seen) == len(set(seen)) == 124
            history = http.get(
                GRAPH_PATH,
                params={
                    "companion_id": "c_mochi",
                    "history": "true",
                },
                headers=AUTH,
            ).json()
            assert len(history["edges"]) == 125
            ended = next(edge for edge in history["edges"] if edge["object"] == "物品0")
            assert ended["valid_to"] is not None
            assert history["history"] is True
            assert "秘密" not in str(history)
            assert "敏感资料" not in str(history)
            assert (
                http.get(GRAPH_PATH, params={"cursor": "garbage"}, headers=AUTH).status_code == 422
            )
    finally:
        graph.close()
