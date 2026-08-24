"""Every Owner-facing memory route answers only to the Host's credential.

The surface this guards is a person's memory, on a port that until now checked
nothing. It was reachable by anything running on the machine, and a token for it
existed in the settings that no code read — a credential nobody checks is worse
than none, because it makes the surface look guarded.

What makes this file a gate rather than a list: it **discovers** the routes from
the factory that mounts them, so a route added tomorrow is covered by a test
written today. The factory is the only way into ``/api/memory/v1``, and it
refuses paths outside that prefix, so "mounted without the guard" is not
expressible.

The MCP transports on the same app are deliberately out of scope — the agent
speaks to those, and gating them is a separate change with a separate consumer.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from eidolon.memory.entrypoints.memory_api import API_PREFIX, memory_api_routes

TOKEN = "memory-api-token"


async def _answer(_request):
    return JSONResponse({"reached": True})


def _app(*, service_token: str) -> Starlette:
    """Two routes, so the guard is shown applying per route rather than once."""

    return Starlette(
        routes=memory_api_routes(
            service_token=service_token,
            routes={
                f"{API_PREFIX}/first": (_answer, ["GET"]),
                f"{API_PREFIX}/second": (_answer, ["POST"]),
            },
        )
    )


def _paths() -> list[tuple[str, str]]:
    return [(f"{API_PREFIX}/first", "GET"), (f"{API_PREFIX}/second", "POST")]


def test_no_route_answers_without_the_credential() -> None:
    with TestClient(_app(service_token=TOKEN)) as http:
        for path, method in _paths():
            response = http.request(method, path)
            assert response.status_code == 401, f"{method} {path} answered anonymously"
            assert "reached" not in response.text


def test_no_route_accepts_a_different_credential() -> None:
    """A token that is not this Host's is a refusal, not a fallback."""
    with TestClient(_app(service_token=TOKEN)) as http:
        for path, method in _paths():
            response = http.request(
                method, path, headers={"Authorization": "Bearer not-the-token"}
            )
            assert response.status_code == 401, f"{method} {path} took any token"


def test_an_unconfigured_host_cannot_answer_rather_than_answering_everyone() -> None:
    """The failure mode of a missing secret must not be an open door.

    503, not 401: the caller has nothing to fix, and a Host part-way through
    provisioning should refuse to serve a person's memory rather than serve it
    to whatever else is on the machine.
    """
    with TestClient(_app(service_token="")) as http:
        for path, method in _paths():
            response = http.request(
                method, path, headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert response.status_code == 503, f"{method} {path} answered unconfigured"


def test_the_credential_is_compared_whole() -> None:
    with TestClient(_app(service_token=TOKEN)) as http:
        for header in (
            f"Bearer {TOKEN[:-1]}",
            f"Bearer {TOKEN} ",
            f"Basic {TOKEN}",
            TOKEN,
            "Bearer",
            "",
        ):
            headers = {"Authorization": header} if header else {}
            response = http.get(f"{API_PREFIX}/first", headers=headers)
            assert response.status_code == 401, f"accepted {header!r}"


def test_a_presented_credential_reaches_the_handler() -> None:
    """Otherwise the tests above would pass on a surface that answers nothing."""
    with TestClient(_app(service_token=TOKEN)) as http:
        response = http.get(
            f"{API_PREFIX}/first", headers={"Authorization": f"Bearer {TOKEN}"}
        )

    assert response.status_code == 200
    assert response.json() == {"reached": True}


def test_the_factory_refuses_to_mount_outside_its_prefix() -> None:
    """The guard is the factory, so the factory has to own a known prefix.

    A route slipped in at another path would be ungated *and* look mounted by
    the thing that gates. Refusing it here is what keeps "everything under this
    prefix is guarded" true by construction rather than by review.
    """
    with pytest.raises(ValueError, match=API_PREFIX):
        memory_api_routes(
            service_token=TOKEN, routes={"/api/other/v1/thing": (_answer, ["GET"])}
        )


def test_every_real_owner_route_is_mounted_through_the_factory() -> None:
    """The routes the process actually serves, at the real call site.

    The tests above use stand-ins so they keep working as routes are added; this
    one walks what ``owner_memory_routes`` produces — the same function the
    runner calls — and asserts none of them is the exception. There is one
    mounting path, so this is the whole surface rather than a sample of it.
    """
    from eidolon.memory.entrypoints.owner_memory_http import owner_memory_routes

    class _Service:
        async def runtime_for(self, _context):
            raise AssertionError("authentication must fail before any lookup")

    routes = owner_memory_routes(
        service=_Service(),  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
        memory_space_id="realm_primary",
        service_token=TOKEN,
    )
    assert routes, "no Owner-facing routes found; this gate would pass vacuously"

    app = Starlette(routes=routes)
    with TestClient(app) as http:
        for route in routes:
            path = route.path
            anonymous = http.get(path)
            wrong = http.get(path, headers={"Authorization": "Bearer other"})
            assert (anonymous.status_code, wrong.status_code) == (401, 401), path
