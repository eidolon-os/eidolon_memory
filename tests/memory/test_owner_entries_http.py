"""「今日记住了什么」 — recent entries, and the ways a time read lies quietly.

This read is the one most able to look correct while being wrong, because a
person cannot tell a missing entry from an entry that was never recorded. So the
tests are about the boundaries of the window and about what the answer admits:

- the day is the caller's, not this process's: it does not know where the person
  is, and a guessed timezone answers for the wrong day;
- an entry with no usable time is counted, not floated to the top or buried;
- the page ending and the scan stopping are different facts, and both are said;
- the same visibility policy as every other read on this surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from eidolon_memory_contracts import OWNER_AUDIENCE, companion_audience
from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.domain.wings import CANONICAL_WINGS
from eidolon.memory.entrypoints.owner_memory_http import (
    ENTRIES_PATH,
    owner_memory_routes,
)

TOKEN = "memory-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPACE = "realm_owner_one"
MOCHI = "c_mochi"
NORI = "c_nori"

NOON = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
MORNING = NOON - timedelta(hours=4)
YESTERDAY = NOON - timedelta(days=1)


class _Record:
    def __init__(
        self,
        key: str,
        *,
        when: datetime | None,
        value: str = "内容",
        wing: str = "Wing_Life",
        room: str = "饮食",
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
        self.memory_time = when
        self.memory_time_source = "occurred_at" if when else None


class _Backend:
    def __init__(self, records: list[_Record]) -> None:
        self._records = records

    async def get_all(self, memory_space_id, *, limit=None, offset=None):
        rows = self._records[(offset or 0) :]
        return rows[:limit] if limit is not None else rows


class _Runtime:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        self.palace_path = "/tmp/palace"


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
    wings = CANONICAL_WINGS


def _client(records: list[_Record], *, fails: bool = False):
    service = _Service(_Backend(records), fails=fails)
    app = Starlette(
        routes=owner_memory_routes(
            service=service,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
            memory_space_id=SPACE,
            owner_id="owner-1",
            service_token=TOKEN,
        )
    )
    return TestClient(app), service


def _since(moment: datetime) -> str:
    """Encoded, because a "+" in a query string means a space.

    The offset has to survive the URL; an unencoded one arrives mangled and the
    route refuses it. Real clients build query parameters rather than strings,
    and there is a test below for the mangled case.
    """

    return quote(moment.isoformat(), safe="")


def test_entries_come_back_newest_first_within_the_window() -> None:
    http, _service = _client(
        [
            _Record("drawer_old", when=YESTERDAY),
            _Record("drawer_morning", when=MORNING),
            _Record("drawer_noon", when=NOON),
        ]
    )
    with http:
        body = http.get(
            f"{ENTRIES_PATH}?since={_since(MORNING)}", headers=AUTH
        ).json()

    assert [entry["entry_id"] for entry in body["entries"]] == [
        "drawer_noon",
        "drawer_morning",
    ]
    assert body["entry_count"] == 2
    assert body["since"] == MORNING.isoformat()


def test_the_window_includes_its_own_boundary() -> None:
    """"Since noon" includes what was recorded at noon.

    An exclusive boundary loses an entry every time a client asks for "since the
    last time I looked", which is the ordinary way this read is used.
    """
    http, _service = _client([_Record("drawer_noon", when=NOON)])
    with http:
        body = http.get(f"{ENTRIES_PATH}?since={_since(NOON)}", headers=AUTH).json()

    assert [entry["entry_id"] for entry in body["entries"]] == ["drawer_noon"]


def test_the_day_belongs_to_the_caller() -> None:
    """This process does not know where the person is.

    A default of "today in UTC" would answer for the wrong day for most of the
    world, and it would do it silently.
    """
    http, _service = _client([_Record("drawer_noon", when=NOON)])
    with http:
        missing = http.get(ENTRIES_PATH, headers=AUTH)
        naive = http.get(f"{ENTRIES_PATH}?since=2026-08-24T00:00:00", headers=AUTH)
        nonsense = http.get(f"{ENTRIES_PATH}?since=today", headers=AUTH)

    assert missing.status_code == 422
    # Naive is refused rather than assumed UTC: the assumption is invisible and
    # wrong by up to a day.
    assert naive.status_code == 422
    assert nonsense.status_code == 422


def test_an_unencoded_offset_says_what_went_wrong() -> None:
    """The mistake every first client makes, once.

    A "+" in a query string is a space, so an unencoded offset arrives mangled
    and looks like a malformed instant. Repairing it here would be this boundary
    guessing at a caller's encoding; naming it costs a sentence.
    """
    http, _service = _client([])
    with http:
        response = http.get(f"{ENTRIES_PATH}?since={NOON.isoformat()}", headers=AUTH)

    assert response.status_code == 422
    assert "unencoded" in response.json()["detail"]


def test_an_entry_with_no_usable_time_is_counted_not_placed() -> None:
    """Guessing either way looks like the read working.

    "Now" floats it to the top of every day's list; the epoch buries it forever.
    Counted, so a person whose entry never appears can find out why.
    """
    http, _service = _client(
        [_Record("drawer_noon", when=NOON), _Record("drawer_unknown", when=None)]
    )
    with http:
        body = http.get(f"{ENTRIES_PATH}?since={_since(YESTERDAY)}", headers=AUTH).json()

    assert [entry["entry_id"] for entry in body["entries"]] == ["drawer_noon"]
    assert body["undated_count"] == 1


def test_a_full_page_says_there_is_more_in_the_window() -> None:
    """Distinct from the scan stopping: one is this answer, the other the palace."""
    http, _service = _client(
        [
            _Record(f"drawer_{index}", when=NOON - timedelta(minutes=index))
            for index in range(5)
        ]
    )
    with http:
        body = http.get(
            f"{ENTRIES_PATH}?since={_since(YESTERDAY)}&limit=2", headers=AUTH
        ).json()

    assert body["entry_count"] == 2
    assert body["more_in_window"] is True
    assert body["truncated"] is False


def test_nothing_in_the_window_is_not_an_error() -> None:
    """A quiet day is a real answer, and a common one."""
    http, _service = _client([_Record("drawer_old", when=YESTERDAY)])
    with http:
        body = http.get(f"{ENTRIES_PATH}?since={_since(NOON)}", headers=AUTH).json()

    assert body["entries"] == []
    assert body["entry_count"] == 0
    assert body["undated_count"] == 0


def test_the_privacy_wing_stays_out_of_the_day() -> None:
    """The same policy as the library and as recall.

    A read that applied it on one screen and not the next would make what a
    person sees depend on which one they opened.
    """
    http, _service = _client(
        [
            _Record("drawer_noon", when=NOON),
            _Record("drawer_private", when=NOON, wing="Wing_Privacy"),
            _Record("drawer_quiet", when=NOON, privacy="do_not_recall"),
        ]
    )
    with http:
        body = http.get(f"{ENTRIES_PATH}?since={_since(MORNING)}", headers=AUTH).json()

    assert [entry["entry_id"] for entry in body["entries"]] == ["drawer_noon"]


def test_one_companions_private_entry_is_not_in_anothers_day() -> None:
    records = [
        _Record("drawer_shared", when=NOON, audience=OWNER_AUDIENCE),
        _Record("drawer_mochis", when=NOON, audience=companion_audience(MOCHI)),
    ]
    http, _service = _client(records)
    with http:
        mine = http.get(
            f"{ENTRIES_PATH}?since={_since(MORNING)}&companion_id={MOCHI}", headers=AUTH
        ).json()
        theirs = http.get(
            f"{ENTRIES_PATH}?since={_since(MORNING)}&companion_id={NORI}", headers=AUTH
        ).json()

    assert {entry["entry_id"] for entry in mine["entries"]} == {
        "drawer_shared",
        "drawer_mochis",
    }
    assert [entry["entry_id"] for entry in theirs["entries"]] == ["drawer_shared"]


def test_an_entry_says_where_its_time_came_from() -> None:
    """An Eidolon filing something under the wrong day is a real complaint.

    The field a person never reads is the one that makes it answerable.
    """
    http, _service = _client([_Record("drawer_noon", when=NOON)])
    with http:
        body = http.get(f"{ENTRIES_PATH}?since={_since(MORNING)}", headers=AUTH).json()

    assert body["entries"][0]["recorded_at"] == NOON.isoformat()
    assert body["entries"][0]["recorded_at_source"] == "occurred_at"


def test_a_long_entry_is_shortened_rather_than_shown_whole() -> None:
    """A list is a list. One entry that scrolls pushes the rest of the day off it."""
    http, _service = _client(
        [_Record("drawer_long", when=NOON, value="很长的内容" * 60)]
    )
    with http:
        body = http.get(f"{ENTRIES_PATH}?since={_since(MORNING)}", headers=AUTH).json()

    preview = body["entries"][0]["preview"]
    assert len(preview) <= 160
    assert preview.endswith("…")


def test_a_limit_is_bounded_rather_than_believed() -> None:
    http, _service = _client(
        [
            _Record(f"drawer_{index}", when=NOON - timedelta(minutes=index))
            for index in range(3)
        ]
    )
    with http:
        huge = http.get(
            f"{ENTRIES_PATH}?since={_since(YESTERDAY)}&limit=99999", headers=AUTH
        ).json()
        zero = http.get(
            f"{ENTRIES_PATH}?since={_since(YESTERDAY)}&limit=0", headers=AUTH
        ).json()
        text = http.get(
            f"{ENTRIES_PATH}?since={_since(YESTERDAY)}&limit=many", headers=AUTH
        )

    assert huge["entry_count"] == 3
    assert zero["entry_count"] == 1
    assert text.status_code == 422


def test_a_memory_that_cannot_be_read_is_not_a_quiet_day() -> None:
    http, _service = _client([], fails=True)
    with http:
        response = http.get(f"{ENTRIES_PATH}?since={_since(MORNING)}", headers=AUTH)

    assert response.status_code == 503
    assert "entries" not in response.json()
