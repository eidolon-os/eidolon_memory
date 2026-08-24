"""「只让它记得」— saying which of my Eidolons a memory belongs to.

The audience axis has had a read side since Phase 2: every read on this surface
filters by it, and a Companion cannot recall what belongs to another. What it did
not have was a way to *ask*. Until this route existed, "只让它记得" was something
the system could enforce and nobody could request — which is the shape of a
feature that looks finished and is half of one.

The design question worth pinning here is why this is **one step** when forgetting
is two. A forget resolves words into a set, so between the preview and the
confirm the words could match something the person never saw; the token exists to
bind exactly what was shown. Here the subject is one entry they were looking at,
named in the path. Nothing is resolved, so there is nothing to bind — and nothing
becomes unrecallable, because the memory is still recalled in full by the
Companion it now belongs to, and moving it back is the same call.

So the tests are about the things that *can* still go wrong: a key that is not a
memory, a Companion id that cannot be an audience token, a Host that cannot
publish the write at all, and a route that claims a change is done when it has
only been accepted.
"""

from __future__ import annotations

from typing import Any

import pytest
from eidolon_memory_contracts import AudienceMutationCommand
from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.entrypoints import owner_memory_http
from eidolon.memory.entrypoints.owner_memory_http import owner_memory_routes

TOKEN = "memory-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPACE = "realm_owner_one"
MOCHI = "c_mochi"


def _path(entry_id: str) -> str:
    return f"/api/memory/v1/entries/{entry_id}/audience"


class _Runtime:
    backend = object()
    palace_path = "/tmp/palace"


class _Service:
    privacy_signer = None

    async def runtime_for(self, context: Any) -> _Runtime:
        return _Runtime()


class _Settings:
    wings: list[Any] = []


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_publisher: bool = True,
    outcome: dict[str, Any] | None = None,
    publish_raises: bool = False,
):
    published: list[AudienceMutationCommand] = []

    async def _publish(_publisher, _status, command, *, wait_seconds):
        if publish_raises:
            raise RuntimeError("the bus is gone")
        published.append(command)
        return outcome if outcome is not None else {"status": "applied"}

    monkeypatch.setattr(owner_memory_http, "publish_with_status", _publish)
    app = Starlette(
        routes=owner_memory_routes(
            service=_Service(),  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
            memory_space_id=SPACE,
            owner_id="owner-1",
            service_token=TOKEN,
            command_publisher=object() if with_publisher else None,
            command_status=None,
        )
    )
    return TestClient(app), published


def test_a_memory_can_be_given_to_one_companion(monkeypatch) -> None:
    http, published = _client(monkeypatch)
    with http:
        body = http.put(
            _path("drawer_1"), headers=AUTH, json={"companion_id": MOCHI}
        ).json()

    assert body["audience"] == f"companion:{MOCHI}"
    # Echoed, so a client never has to parse the audience token to know which
    # Eidolon it named.
    assert body["companion_id"] == MOCHI
    command = published[0]
    assert command.drawer_ids == ["drawer_1"]
    assert command.audience == f"companion:{MOCHI}"
    # A person asked through a management surface; the Eidolon did not decide.
    assert command.issuer == "admin"


def test_naming_no_companion_gives_it_back_to_everyone(monkeypatch) -> None:
    """The way back, and it is the same call.

    Said as an absence rather than the string "owner" so a client cannot
    accidentally hand it a Companion named that.
    """
    http, published = _client(monkeypatch)
    with http:
        body = http.put(_path("drawer_1"), headers=AUTH, json={}).json()

    assert body["audience"] == "owner"
    assert body["companion_id"] == ""
    assert published[0].audience == "owner"


def test_the_same_call_twice_leaves_the_same_memory_in_the_same_audience(
    monkeypatch,
) -> None:
    """Which is what lets a client retry a request it never saw the answer to."""

    http, published = _client(monkeypatch)
    with http:
        first = http.put(_path("drawer_1"), headers=AUTH, json={"companion_id": MOCHI})
        second = http.put(_path("drawer_1"), headers=AUTH, json={"companion_id": MOCHI})

    assert first.json()["audience"] == second.json()["audience"]
    assert [command.drawer_ids for command in published] == [["drawer_1"], ["drawer_1"]]


def test_a_key_that_cannot_name_a_memory_is_refused(monkeypatch) -> None:
    """Guessing at what the caller meant is how one memory's audience gets
    written onto another's."""

    http, published = _client(monkeypatch)
    with http:
        answered = http.put(_path("1"), headers=AUTH, json={"companion_id": MOCHI})

    assert answered.status_code == 422
    assert published == []


def test_a_companion_id_that_cannot_be_an_audience_is_refused(monkeypatch) -> None:
    """The token ends up in metadata, SQL parameters and vector-store filters,
    so the contract's own vocabulary decides — not this route."""

    http, published = _client(monkeypatch)
    with http:
        answered = http.put(
            _path("drawer_1"), headers=AUTH, json={"companion_id": "c mochi/../x"}
        )

    assert answered.status_code == 422
    assert published == []


def test_a_body_that_is_not_an_object_is_refused(monkeypatch) -> None:
    http, published = _client(monkeypatch)
    with http:
        assert http.put(_path("drawer_1"), headers=AUTH, content=b"nonsense").status_code == 422
        assert http.put(_path("drawer_1"), headers=AUTH, json=[MOCHI]).status_code == 422
    assert published == []


def test_a_host_that_cannot_publish_does_not_offer_the_route(monkeypatch) -> None:
    """Its absence is discoverable; a route that could only ever fail is a
    promise this Host cannot honour."""

    http, _published = _client(monkeypatch, with_publisher=False)
    with http:
        answered = http.put(
            _path("drawer_1"), headers=AUTH, json={"companion_id": MOCHI}
        )

    # 404 rather than 405: the path is not mounted at all, which is exactly the
    # discoverability that makes a missing capability legible to a client.
    assert answered.status_code == 404


def test_an_accepted_write_is_not_reported_as_applied(monkeypatch) -> None:
    """Publishing is durable; applying is a projection still catching up.

    The comfortable lie here would be to say the memory has moved because the
    command went out.
    """
    http, _published = _client(monkeypatch, outcome={"status": "accepted"})
    with http:
        body = http.put(
            _path("drawer_1"), headers=AUTH, json={"companion_id": MOCHI}
        ).json()

    assert body["status"] == "accepted"


def test_a_write_that_could_not_be_published_says_so(monkeypatch) -> None:
    http, _published = _client(monkeypatch, publish_raises=True)
    with http:
        answered = http.put(
            _path("drawer_1"), headers=AUTH, json={"companion_id": MOCHI}
        )

    assert answered.status_code == 503


def test_the_route_is_credential_gated_like_every_other_owner_write(monkeypatch) -> None:
    http, published = _client(monkeypatch)
    with http:
        assert http.put(_path("drawer_1"), json={"companion_id": MOCHI}).status_code == 401
    assert published == []
