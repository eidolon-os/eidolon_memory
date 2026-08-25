"""「导出」 — the copy a person takes away, and what it refuses to shorten.

Two artefacts were both called "export" in the plan and they have almost nothing
in common. The Host backup is a copy of the palace: opaque, restorable, and
proof against a lost disk. This is the other one — the file a person keeps so
they are not locked in — and its whole value is being *complete* and *readable*.

So the tests here are the mirror image of the day list's. There, shortening is
right and an undated entry belongs to no day. Here, shortening is data loss:

- statements travel whole, not as previews;
- a record that carries no usable time is in the file, at the end, and counted;
- the visibility policy is still recall's, because an export must not be a way
  to see what an Eidolon cannot;
- a bounded scan that stopped says so, because a file that is silently part of a
  memory is worse than one that says it is part.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from eidolon_memory_contracts import OWNER_AUDIENCE, companion_audience
from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.domain.wings import CANONICAL_WINGS
from eidolon.memory.entrypoints.owner_memory_http import (
    EXPORT_PATH,
    owner_memory_routes,
)

TOKEN = "memory-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPACE = "realm_owner_one"
MOCHI = "c_mochi"

NOON = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
MORNING = NOON - timedelta(hours=4)

LONG = "他早上喝了乌龙茶，" * 40


class _Record:
    def __init__(
        self,
        key: str,
        *,
        when: datetime | None = NOON,
        value: str = "内容",
        wing: str = "Wing_Life",
        room: str = "饮食",
        memory_type: str = "event",
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
            "memory_type": memory_type,
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


def test_the_file_carries_statements_whole() -> None:
    """The library and the day list shorten; this is the copy.

    A preview in an export is data loss that looks like a working read.
    """
    http, _service = _client([_Record("drawer_long", value=LONG)])
    with http:
        body = http.get(EXPORT_PATH, headers=AUTH).json()

    assert body["records"][0]["value"] == LONG
    assert "…" not in body["records"][0]["value"]


def test_records_are_newest_first_and_the_undated_are_last_rather_than_absent() -> None:
    """Unlike the day list, where an undated entry belongs to no day.

    Leaving one out of the copy is losing it, so it is at the end and counted —
    a person reading their file can see why some of it carries no date.
    """
    http, _service = _client(
        [
            _Record("drawer_undated", when=None),
            _Record("drawer_morning", when=MORNING),
            _Record("drawer_noon", when=NOON),
        ]
    )
    with http:
        body = http.get(EXPORT_PATH, headers=AUTH).json()

    assert [record["entry_id"] for record in body["records"]] == [
        "drawer_noon",
        "drawer_morning",
        "drawer_undated",
    ]
    assert body["record_count"] == 3
    assert body["undated_count"] == 1
    assert body["records"][-1]["recorded_at"] == ""


def test_the_file_says_where_each_memory_sits_and_what_kind_it_is() -> None:
    http, _service = _client([_Record("drawer_1", wing="Wing_Profile", room="偏好")])
    with http:
        record = http.get(EXPORT_PATH, headers=AUTH).json()["records"][0]

    assert record["wing_id"] == "Wing_Profile"
    assert record["room_id"] == "偏好"
    assert record["memory_type"] == "event"
    assert record["recorded_at_source"] == "occurred_at"


def test_the_file_holds_no_internal_metadata() -> None:
    """A named set travels; the rest stays in.

    Handing over the whole mapping would make routing keys part of a contract a
    person's saved file depends on, and would carry details that mean nothing to
    them and something to whoever reads the file next.

    ``audience`` is in the named set, and that is the distinction this test now
    draws rather than the one it used to: it is not internal bookkeeping that
    leaked, it is the person's own decision about which of their Eidolons was
    told a thing. A file carrying a companion-private memory without saying so
    would be less true than the memory it copies (§16).
    """
    # Owner-visible, so it is in the file at all — and still carrying the
    # internal keys the file must not learn.
    http, _service = _client([_Record("drawer_1", audience=OWNER_AUDIENCE)])
    with http:
        record = http.get(EXPORT_PATH, headers=AUTH).json()["records"][0]

    assert "metadata" not in record
    assert "memory_space_id" not in record
    assert set(record) == {
        "entry_id",
        "recorded_at",
        "recorded_at_source",
        "wing_id",
        "room_id",
        "memory_type",
        "value",
        "audience",
    }


def test_an_export_cannot_see_past_a_boundary_it_is_inside() -> None:
    """One axis behaves differently here, and only one.

    Privacy, space and device rules are boundaries drawn *around* the person, so
    an export must not be the way past them — it is the read with the widest
    reach and would otherwise undo every rule above it. ``do_not_recall`` stays
    out of both files below for that reason.

    The audience axis is a boundary drawn *between the person's own Eidolons*,
    and the Owner is not one of them. Somebody who marked a memory 「只让它记得」
    narrowed who is told; they did not ask to lose it from their own copy. So
    the request that names no Companion is the Owner asking for their own file
    and carries every audience in the Realm (§16), while naming a Companion
    means "what this Eidolon can recall" and keeps the recall predicate exactly
    — the widening must not become a way around isolation.
    """

    http, _service = _client(
        [
            _Record("drawer_owner", audience=OWNER_AUDIENCE),
            _Record("drawer_mochi", audience=companion_audience(MOCHI)),
            _Record("drawer_nori", audience=companion_audience("c_nori")),
            _Record("drawer_private", wing="Wing_Privacy", privacy="do_not_recall"),
        ]
    )
    with http:
        owner_view = http.get(EXPORT_PATH, headers=AUTH).json()
        with_mochi = http.get(
            f"{EXPORT_PATH}?companion_id={MOCHI}", headers=AUTH
        ).json()

    # Named per record, not merely present somewhere: the point of carrying the
    # audience is that the person can tell which of their Eidolons knows a
    # thing, which is exactly what they decided when they marked it.
    assert {record["entry_id"]: record["audience"] for record in owner_view["records"]} == {
        "drawer_owner": OWNER_AUDIENCE,
        "drawer_mochi": companion_audience(MOCHI),
        "drawer_nori": companion_audience("c_nori"),
    }
    assert {record["entry_id"] for record in with_mochi["records"]} == {
        "drawer_owner",
        "drawer_mochi",
    }


def test_a_scan_that_stopped_says_so() -> None:
    """A file that is silently part of a memory is worse than one that says it is."""

    from eidolon.memory.entrypoints import owner_memory_http as module

    http, _service = _client([_Record(f"drawer_{index}") for index in range(5)])
    original = module.DEFAULT_EXPORT_SCAN
    module.DEFAULT_EXPORT_SCAN = 2
    try:
        with http:
            body = http.get(EXPORT_PATH, headers=AUTH).json()
    finally:
        module.DEFAULT_EXPORT_SCAN = original

    assert body["truncated"] is True
    assert body["record_count"] == 2


def test_a_memory_that_could_not_be_read_is_not_an_empty_file() -> None:
    """Someone who saved it would believe their Eidolon remembers nothing."""

    http, _service = _client([_Record("drawer_1")], fails=True)
    with http:
        response = http.get(EXPORT_PATH, headers=AUTH)

    assert response.status_code == 503


def test_the_export_is_credential_gated_like_every_other_owner_read() -> None:
    http, _service = _client([_Record("drawer_1")])
    with http:
        assert http.get(EXPORT_PATH).status_code == 401


def test_the_file_says_when_it_was_taken() -> None:
    """Two exports of the same memory differ, and a file with no instant cannot
    be told apart from a stale one."""

    http, _service = _client([_Record("drawer_1")])
    with http:
        body = http.get(EXPORT_PATH, headers=AUTH).json()

    assert body["operation"] == "memory.export"
    assert datetime.fromisoformat(body["taken_at"]).tzinfo is not None
