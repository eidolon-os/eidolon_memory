"""What a person sees when they look at their own memory, and what they must not.

The base for this read is an operator tool: ``scan_records`` enumerates the whole
palace with no ``where`` clause and no visibility policy at all, which is right
for someone debugging a palace and wrong for the person who owns it. So the
thing worth testing is not that a tree comes back — it is that the tree is
filtered by the *same* policy recall uses, and that operator vocabulary does not
leak through with it.

Two ways this could be got wrong, both silent:

- pass the operator snapshot straight through, and a person is shown their own
  privacy wing, plus a filesystem path and a steward mode;
- filter with a second policy written here, and browse and recall drift until
  what a person can see depends on which screen they opened.
"""

from __future__ import annotations

from typing import Any

import pytest
from eidolon_memory_contracts import OWNER_AUDIENCE, companion_audience
from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.domain.wings import CANONICAL_WINGS
from eidolon.memory.entrypoints.owner_memory_http import (
    BROWSE_PATH,
    owner_memory_routes,
)

TOKEN = "memory-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPACE = "realm_owner_one"
MOCHI = "c_mochi"
NORI = "c_nori"


class _Record:
    """A drawer as the backend hands it back."""

    def __init__(
        self,
        key: str,
        *,
        wing: str,
        room: str,
        value: str = "内容",
        audience: str | None = None,
        privacy: str | None = None,
    ) -> None:
        self.key = key
        self.value = value
        self.memory_space_id = SPACE
        self.metadata: dict[str, Any] = {
            "memory_space_id": SPACE,
            "wing": wing,
            "room": room,
        }
        if audience is not None:
            self.metadata["audience"] = audience
        if privacy is not None:
            self.metadata["privacy"] = privacy
        self.extensions: dict[str, Any] = {}


class _Backend:
    def __init__(self, records: list[_Record]) -> None:
        self._records = records
        self.scans: list[tuple[str, int | None, int | None]] = []

    async def get_all(self, memory_space_id, *, limit=None, offset=None):
        self.scans.append((memory_space_id, limit, offset))
        start = offset or 0
        rows = self._records[start:]
        return rows[:limit] if limit is not None else rows


class _Runtime:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        self.palace_path = "/var/lib/eidolon/palaces/owner-one"


class _Service:
    def __init__(self, backend: _Backend, *, fails: bool = False) -> None:
        self._runtime = _Runtime(backend)
        self.fails = fails
        self.contexts: list[Any] = []

    async def runtime_for(self, context: Any) -> _Runtime:
        self.contexts.append(context)
        if self.fails:
            raise RuntimeError("space is not resolvable")
        return self._runtime


class _Settings:
    """Only what the builder reads: the configured wings."""

    wings = CANONICAL_WINGS


def _client(records: list[_Record], *, fails: bool = False):
    backend = _Backend(records)
    service = _Service(backend, fails=fails)
    app = Starlette(
        routes=owner_memory_routes(
            service=service,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
            memory_space_id=SPACE,
            owner_id="owner-1",
            service_token=TOKEN,
        )
    )
    return TestClient(app), service, backend


def _wing(body: dict, wing_id: str) -> dict | None:
    return next((w for w in body["wings"] if w["wing_id"] == wing_id), None)


def test_the_owner_sees_their_memory_by_wing_and_room() -> None:
    http, _service, _backend = _client(
        [
            _Record("d1", wing="Wing_Life", room="饮食"),
            _Record("d2", wing="Wing_Life", room="饮食"),
            _Record("d3", wing="Wing_Life", room="出行"),
        ]
    )
    with http:
        body = http.get(BROWSE_PATH, headers=AUTH).json()

    assert body["operation"] == "memory.browse"
    assert body["entry_count"] == 3
    preference = _wing(body, "Wing_Life")
    assert preference is not None
    # Named, not identified: the wing id is machine vocabulary.
    assert preference["display_name"]
    assert {room["room_id"]: room["drawer_count"] for room in preference["rooms"]} == {
        "饮食": 2,
        "出行": 1,
    }


def test_the_privacy_wing_is_counted_but_not_listed() -> None:
    """"Do not bring this up" has to keep meaning that on this screen too.

    Counted rather than erased: "there are 2 things here you asked me not to
    raise" is true and is a fact about their own memory. A total that quietly
    differs from what is listed is neither.
    """
    http, _service, _backend = _client(
        [
            _Record("d1", wing="Wing_Life", room="饮食"),
            _Record("p1", wing="Wing_Privacy", room="别提"),
            _Record("p2", wing="Wing_Privacy", room="别提"),
        ]
    )
    with http:
        body = http.get(BROWSE_PATH, headers=AUTH).json()

    assert _wing(body, "Wing_Privacy") is None
    assert body["entry_count"] == 1
    assert body["withheld_count"] == 2


def test_a_do_not_recall_drawer_is_not_shown_either() -> None:
    """The record-level flag, not just the wing.

    A person can mark one thing rather than a whole category, and browse has to
    respect the same mark recall does.
    """
    http, _service, _backend = _client(
        [
            _Record("d1", wing="Wing_Life", room="饮食"),
            _Record("d2", wing="Wing_Life", room="饮食", privacy="do_not_recall"),
        ]
    )
    with http:
        body = http.get(BROWSE_PATH, headers=AUTH).json()

    assert body["entry_count"] == 1
    assert body["withheld_count"] == 1
    assert [d["key"] for d in _wing(body, "Wing_Life")["rooms"][0]["drawers_preview"]] == ["d1"]


def test_one_companions_private_statement_is_not_browsable_by_another() -> None:
    """The audience axis, on this surface too.

    Nothing writes a companion audience today, so this is the mechanism being
    kept ready rather than a behaviour in use — and it is exactly the kind of
    filter that gets applied on one screen and forgotten on the next.
    """
    records = [
        _Record("shared", wing="Wing_Life", room="饮食", audience=OWNER_AUDIENCE),
        _Record(
            "mochis",
            wing="Wing_Life",
            room="饮食",
            audience=companion_audience(MOCHI),
        ),
    ]

    http, _service, _backend = _client(records)
    with http:
        mine = http.get(f"{BROWSE_PATH}?companion_id={MOCHI}", headers=AUTH).json()
        theirs = http.get(f"{BROWSE_PATH}?companion_id={NORI}", headers=AUTH).json()
        anonymous = http.get(BROWSE_PATH, headers=AUTH).json()

    assert mine["entry_count"] == 2
    assert theirs["entry_count"] == 1
    assert theirs["withheld_count"] == 1
    # No companion named: the Owner layer only, which is the safe direction.
    assert anonymous["entry_count"] == 1


def test_the_answer_carries_no_operator_vocabulary() -> None:
    """The operator snapshot answers a different question, in its own words.

    It reports ``palace_path``, ``steward_mode``, mempalace layer descriptions
    and room-naming conventions. One of those is a filesystem path and none of
    them mean anything to the person whose memory this is; passing the snapshot
    through would have been the shortest way to write this route.
    """
    http, _service, _backend = _client(
        [_Record("d1", wing="Wing_Life", room="饮食")]
    )
    with http:
        body = http.get(BROWSE_PATH, headers=AUTH).json()

    for leaked in ("palace_path", "steward_mode", "layers", "room_naming_conventions"):
        assert leaked not in body
    assert set(body) == {
        "contract_version",
        "operation",
        "memory_space_id",
        "wings",
        "entry_count",
        "withheld_count",
        "truncated",
    }


def test_an_empty_wing_is_not_shown() -> None:
    """Nine empty categories tell a person nothing about what is remembered."""
    http, _service, _backend = _client(
        [_Record("d1", wing="Wing_Life", room="饮食")]
    )
    with http:
        body = http.get(BROWSE_PATH, headers=AUTH).json()

    assert [wing["wing_id"] for wing in body["wings"]] == ["Wing_Life"]


def test_a_wing_the_configuration_never_heard_of_is_still_shown() -> None:
    """Something is in there; not naming it would be hiding it.

    It comes last and without a display name, which is honest — this build does
    not know what to call it.
    """
    http, _service, _backend = _client(
        [
            _Record("d1", wing="Wing_Life", room="饮食"),
            _Record("x1", wing="Wing_FromALaterRelease", room="?"),
        ]
    )
    with http:
        body = http.get(BROWSE_PATH, headers=AUTH).json()

    unknown = _wing(body, "Wing_FromALaterRelease")
    assert unknown is not None
    assert unknown["is_configured"] is False
    assert body["wings"][-1]["wing_id"] == "Wing_FromALaterRelease"


def test_the_scan_is_bounded_and_says_when_it_stopped() -> None:
    """"This is everything" is a claim a bounded read cannot always make."""
    http, _service, backend = _client(
        [_Record(f"d{i}", wing="Wing_Life", room="饮食") for i in range(5)]
    )
    with http:
        bounded = http.get(f"{BROWSE_PATH}?max_records=2", headers=AUTH).json()
        whole = http.get(f"{BROWSE_PATH}?max_records=50", headers=AUTH).json()

    assert bounded["entry_count"] == 2
    assert bounded["truncated"] is True
    assert whole["entry_count"] == 5
    assert whole["truncated"] is False


def test_a_max_records_that_is_not_a_number_is_refused() -> None:
    http, _service, _backend = _client([])
    with http:
        assert http.get(f"{BROWSE_PATH}?max_records=lots", headers=AUTH).status_code == 422


def test_a_memory_that_cannot_be_read_is_not_answered_as_empty() -> None:
    """An empty palace and an unreachable one look the same to a person."""
    http, _service, _backend = _client([], fails=True)
    with http:
        response = http.get(BROWSE_PATH, headers=AUTH)

    assert response.status_code == 503
    assert "wings" not in response.json()


def test_the_space_is_this_process_and_not_the_callers_to_choose() -> None:
    """A query naming a realm would answer for one this caller was never routed to."""
    http, service, _backend = _client(
        [_Record("d1", wing="Wing_Life", room="饮食")]
    )
    with http:
        body = http.get(
            f"{BROWSE_PATH}?memory_space_id=someone_elses", headers=AUTH
        ).json()

    assert body["memory_space_id"] == SPACE
    assert all(context.memory_space_id == SPACE for context in service.contexts)


@pytest.mark.parametrize("companion_id", ["", "   "])
def test_a_blank_companion_is_no_companion(companion_id: str) -> None:
    """Not a Companion whose id is the empty string, which would match nothing."""
    http, service, _backend = _client(
        [_Record("d1", wing="Wing_Life", room="饮食")]
    )
    with http:
        http.get(f"{BROWSE_PATH}?companion_id={companion_id}", headers=AUTH)

    assert service.contexts[-1].companion_id is None
