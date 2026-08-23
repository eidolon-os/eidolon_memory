"""The read surface a person's own Host reaches for "what do you remember".

The service and the search itself are covered elsewhere; what is tested here is
the surface: what it refuses, what it never invents, and that it answers with
the same records the agent's tool would see rather than a second opinion.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.entrypoints import recollections_http
from eidolon.memory.entrypoints.recollections_http import (
    DEFAULT_RESULTS,
    MAXIMUM_RESULTS,
    recollections_route,
)


class _Runtime:
    backend = object()
    palace_path = "/tmp/palace"


class _Service:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.contexts: list[Any] = []

    async def runtime_for(self, context: Any) -> _Runtime:
        self.contexts.append(context)
        if self.fails:
            raise RuntimeError("space is not resolvable")
        return _Runtime()


class _Record:
    def __init__(self, text: str) -> None:
        self.text = text


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    service = _Service()
    calls: list[dict[str, Any]] = []

    async def _search(backend, settings, **kwargs):
        calls.append(kwargs)
        return [_Record("他喜欢在下午散步")]

    monkeypatch.setattr(recollections_http, "search_all_wings_mcp_style", _search)
    monkeypatch.setattr(
        recollections_http,
        "wire_record_to_public_dict",
        lambda record: {"text": record.text},
    )
    app = Starlette(
        routes=[
            recollections_route(
                service=service,  # type: ignore[arg-type]
                settings=object(),  # type: ignore[arg-type]
                memory_space_id="realm_primary",
                owner_id="owner-1",
            )
        ]
    )
    with TestClient(app) as http:
        yield http, service, calls


def test_a_question_is_required(client) -> None:
    http, _service, calls = client

    assert http.get("/api/memory/v1/recollections").status_code == 422
    assert http.get("/api/memory/v1/recollections?q=%20%20").status_code == 422
    # Nothing was searched for, so nothing was searched.
    assert calls == []


def test_answers_with_what_the_space_holds(client) -> None:
    http, service, calls = client

    response = http.get("/api/memory/v1/recollections?q=散步")

    assert response.status_code == 200
    body = response.json()
    assert body["operation"] == "memory.recollections"
    assert body["memory_space_id"] == "realm_primary"
    assert body["query"] == "散步"
    assert body["recollections"] == [{"text": "他喜欢在下午散步"}]
    # The space is the one this process serves; a caller cannot name another.
    assert service.contexts[0].memory_realm_id == "realm_primary"
    assert calls[0]["top_k"] == DEFAULT_RESULTS
    # A lookup, not a recall: no wing, no room, no voice shaping.
    assert calls[0]["wing"] is None
    assert calls[0]["room"] is None
    assert calls[0]["for_voice"] is False
    # "Could not look" and "there is nothing" are different answers, and this
    # is the surface where a person asked the question that distinguishes them.
    assert calls[0]["raise_on_degraded"] is True


def test_a_limit_is_bounded_rather_than_believed(client) -> None:
    http, _service, calls = client

    http.get("/api/memory/v1/recollections?q=x&limit=9999")
    http.get("/api/memory/v1/recollections?q=x&limit=0")
    assert [call["top_k"] for call in calls] == [MAXIMUM_RESULTS, 1]

    assert (
        http.get("/api/memory/v1/recollections?q=x&limit=many").status_code == 422
    )


def test_memory_being_unavailable_is_said_rather_than_answered_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list would read as "it remembers nothing about you"."""

    app = Starlette(
        routes=[
            recollections_route(
                service=_Service(fails=True),  # type: ignore[arg-type]
                settings=object(),  # type: ignore[arg-type]
                memory_space_id="realm_primary",
            )
        ]
    )
    with TestClient(app) as http:
        response = http.get("/api/memory/v1/recollections?q=散步")

    assert response.status_code == 503
    assert "recollections" not in response.json()


def test_a_runner_is_spawned_with_the_environment_it_needs_to_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one child that encodes text was the one child without the encoder.

    Palace init and every other subprocess were given the MemPalace embedding
    environment; the runner was given temp isolation only. Nothing failed
    loudly — the embedder asked the model hub for weights that were already on
    disk, a Host with no route out retried for seventy seconds a query, and the
    search reported that it had found nothing.
    """

    from pathlib import Path

    from eidolon.memory.config.memory_settings import MemorySettings
    from eidolon.memory.entrypoints import supervisor as supervisor_module

    settings = MemorySettings()
    settings.embedding.provider = "local"
    settings.embedding.model = "bge-base-zh"
    settings.embedding.model_dir = "/var/lib/eidolon/models/bge-base-zh"

    subject = supervisor_module.Supervisor.__new__(supervisor_module.Supervisor)
    subject._settings = settings

    environment = subject._child_environment(Path("/tmp/palace"), "r_1")

    # The names the child's own factory reads, not the ones settings use.
    assert environment["MEMPALACE_EMBEDDING_MODEL_DIR"] == (
        "/var/lib/eidolon/models/bge-base-zh"
    )
    assert environment["MEMPALACE_EMBEDDING_MODEL"] == "bge-base-zh"
    # And still its own temp isolation, which is what it used to have alone.
    assert environment["TMPDIR"] == environment["SQLITE_TMPDIR"]
