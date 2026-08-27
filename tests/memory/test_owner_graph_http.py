"""The Owner graph is bounded and uses the same audience rule as recall."""

from __future__ import annotations

from typing import Any

from eidolon_memory_contracts import OWNER_AUDIENCE, companion_audience
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


def test_no_companion_means_owner_derived_graph_only() -> None:
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
    assert graph.calls[0]["audiences"] == (OWNER_AUDIENCE,)
