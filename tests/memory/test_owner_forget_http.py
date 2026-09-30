"""Two steps to forget something, and what each step refuses.

The dangerous shape here is a one-step delete driven by a topic: a person types
"忘了上周那件事", something matches, and memory changes. The two steps exist so
that what is confirmed is *what was seen* — the preview binds the exact drawer
ids into a signed token, and the confirm acts on the token rather than
re-resolving the topic.

These tests drive the real candidate scan over the fake backend, a real command
ledger and the real signer, and validate every body with the shared Owner
contract the Host parses with. The previous version replaced the scan with a
fake whose ``to_dict`` returned the Host's shape — which the real class never
produced — so every test here was green while the Host rejected every preview
that found anything.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from eidolon_memory_contracts.owner import (
    OwnerForgetOutcome,
    OwnerForgetPreview,
    OwnerForgetProgress,
)
from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.privacy_confirmation import PrivacyConfirmationSigner
from eidolon.memory.entrypoints.owner_memory_http import (
    FORGET_CONFIRM_PATH,
    FORGET_PREVIEW_PATH,
    FORGET_STATUS_PATH,
    owner_memory_routes,
)
from eidolon.memory.infrastructure.command_status import CommandStatusLedger

TOKEN = "memory-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPACE = "realm_owner_one"


class _Runtime:
    def __init__(self, backend: FakeMemoryBackend) -> None:
        self.backend = backend
        self.palace_path = "/tmp/palace"
        self.ledgers = None


class _Service:
    def __init__(self, backend: FakeMemoryBackend, signer: PrivacyConfirmationSigner) -> None:
        self.privacy_signer = signer
        self._runtime = _Runtime(backend)

    async def runtime_for(self, context: Any) -> _Runtime:
        return self._runtime


class _Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[Any] = []
        self.fail = fail

    async def publish(self, command: Any) -> None:
        if self.fail:
            raise RuntimeError("bus down")
        self.published.append(command)


class _Settings:
    wings: list[Any] = []


async def _canonical(backend: FakeMemoryBackend, key: str, text: str) -> None:
    await backend.ingest_text(
        wing="Wing_Facts",
        room=key,
        text=text,
        metadata={"memory_space_id": SPACE, "assertion_id": f"assert-{key}"},
    )


@pytest.fixture
def ledger(tmp_path: Path) -> CommandStatusLedger:
    return CommandStatusLedger(tmp_path / "cmd.sqlite3", space_id=SPACE)


def _client(
    backend: FakeMemoryBackend,
    ledger: CommandStatusLedger | None,
    *,
    signer: PrivacyConfirmationSigner | None = None,
    publisher: _Publisher | None = None,
    with_publisher: bool = True,
) -> tuple[TestClient, _Publisher]:
    publisher = publisher or _Publisher()
    app = Starlette(
        routes=owner_memory_routes(
            service=_Service(backend, signer or PrivacyConfirmationSigner()),  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
            memory_space_id=SPACE,
            owner_id="owner-1",
            service_token=TOKEN,
            command_publisher=publisher if with_publisher else None,
            command_status=ledger,
        )
    )
    return TestClient(app), publisher


def _preview(http: TestClient, target: str) -> OwnerForgetPreview:
    response = http.post(FORGET_PREVIEW_PATH, params={"target": target}, headers=AUTH)
    assert response.status_code == 200, response.text
    return OwnerForgetPreview.model_validate(response.json())


def _confirm(http: TestClient, token: str) -> OwnerForgetOutcome:
    response = http.post(FORGET_CONFIRM_PATH, params={"confirmation_token": token}, headers=AUTH)
    assert response.status_code == 200, response.text
    return OwnerForgetOutcome.model_validate(response.json())


async def test_a_preview_shows_the_exact_set_and_binds_it(ledger) -> None:
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "上周那件事的记录")
    await _canonical(backend, "drawer_2", "上周那件事的后续")
    http, publisher = _client(backend, ledger)
    with http:
        preview = _preview(http, "上周那件事")

    assert preview.status == "preview"
    assert [entry.entry_id for entry in preview.entries] == ["drawer_1", "drawer_2"]
    # The words a person sees are the memory's own text, not an id.
    assert [entry.preview for entry in preview.entries] == ["上周那件事的记录", "上周那件事的后续"]
    assert preview.confirmation_token and preview.expires_at
    # A preview changes nothing. That is the whole difference from a delete.
    assert publisher.published == []


async def test_more_than_one_match_asks_again_before_deleting(ledger) -> None:
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "那件事一")
    await _canonical(backend, "drawer_2", "那件事二")
    http, _ = _client(backend, ledger)
    with http:
        assert _preview(http, "那件事").needs_confirmation is True


async def test_one_exact_match_does_not(ledger) -> None:
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "乌龙茶")
    http, _ = _client(backend, ledger)
    with http:
        preview = _preview(http, "乌龙茶")

    assert preview.needs_confirmation is False
    assert preview.entries[0].score == 1.0


async def test_nothing_matched_issues_no_token(ledger) -> None:
    """Otherwise a person presses a button that removes nothing and reports success."""
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "乌龙茶")
    http, _ = _client(backend, ledger)
    with http:
        preview = _preview(http, "没有的事")

    assert preview.status == "not_found"
    assert preview.confirmation_token is None


async def test_a_drawer_the_confirm_cannot_remove_is_not_offered(ledger) -> None:
    """A drawer with no ledger assertion has nothing to tombstone.

    The confirm refuses it ("privacy mutation refused a non-canonical drawer"),
    so offering it would mint a token that fails after the person was told the
    forget was accepted.
    """
    backend = FakeMemoryBackend()
    await backend.ingest_text(
        wing="Wing_Facts",
        room="drawer_raw",
        text="工资是两万",
        metadata={"memory_space_id": SPACE},
    )
    await _canonical(backend, "drawer_fact", "工资涨了")
    http, _ = _client(backend, ledger)
    with http:
        preview = _preview(http, "工资")

    assert [entry.entry_id for entry in preview.entries] == ["drawer_fact"]


async def test_too_broad_offers_nothing_rather_than_a_partial_set(ledger) -> None:
    """A partial set would leave the rest believed kept when it was merely unseen."""
    backend = FakeMemoryBackend()
    for index in range(25):
        await _canonical(backend, f"drawer_{index}", f"一切第{index}条")
    http, _ = _client(backend, ledger)
    with http:
        preview = _preview(http, "一切")

    assert preview.status == "too_broad"
    assert preview.confirmation_token is None
    assert preview.entries == ()


async def test_one_character_is_too_broad_not_nothing(ledger) -> None:
    """「茶」 matched nothing only because it was refused, not because it is absent.

    Answering "not found" here would tell the person they never said it.
    """
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "喜欢喝茶")
    http, _ = _client(backend, ledger)
    with http:
        preview = _preview(http, "茶")

    assert preview.status == "too_broad"
    assert preview.detail


def test_a_target_is_required(ledger) -> None:
    http, _ = _client(FakeMemoryBackend(), ledger)
    with http:
        blank = http.post(FORGET_PREVIEW_PATH, headers=AUTH)
        spaces = http.post(FORGET_PREVIEW_PATH, params={"target": "  "}, headers=AUTH)

    assert (blank.status_code, spaces.status_code) == (422, 422)


async def test_confirming_applies_exactly_what_the_preview_bound(ledger) -> None:
    """Not the topic. The token carries the ids; the confirm never re-resolves."""
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "上周那件事的记录")
    await _canonical(backend, "drawer_2", "上周那件事的后续")
    http, publisher = _client(backend, ledger)
    with http:
        preview = _preview(http, "上周那件事")
        # A new memory matching the same words arrives between the two steps.
        await _canonical(backend, "drawer_3", "上周那件事的新消息")
        outcome = _confirm(http, preview.confirmation_token or "")

    assert outcome.entry_count == 2
    assert publisher.published[0].drawer_ids == ["drawer_1", "drawer_2"]
    assert publisher.published[0].action == "delete"
    assert publisher.published[0].target == "上周那件事"


async def test_a_person_asking_is_not_recorded_as_the_eidolon_deciding(ledger) -> None:
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "乌龙茶")
    http, publisher = _client(backend, ledger)
    with http:
        _confirm(http, _preview(http, "乌龙茶").confirmation_token or "")

    assert publisher.published[0].issuer == "admin"


async def test_confirming_twice_is_one_change(ledger) -> None:
    """The same token names the same command, so it is not applied twice.

    With a fresh id per confirm the second delete ran against drawers that were
    already gone, failed three times into the dead-letter queue, and the person
    had been told 「已受理」 both times.
    """
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "乌龙茶")
    http, publisher = _client(backend, ledger)
    with http:
        token = _preview(http, "乌龙茶").confirmation_token or ""
        first = _confirm(http, token)
        second = _confirm(http, token)

    assert first.request_id == second.request_id
    assert first.request_id.startswith("owner-forget-")
    assert len(publisher.published) == 1


async def test_a_failed_forget_is_not_retried_by_the_same_token(ledger) -> None:
    """One decision is one command, and ``failed`` stays failed.

    The ledger never moves a failed command back to accepted, so republishing it
    under the same id would report failure while the retry ran. Trying again is
    a new preview — a new decision with its own id.
    """
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "乌龙茶")
    publisher = _Publisher(fail=True)
    http, _ = _client(backend, ledger, publisher=publisher)
    with http:
        token = _preview(http, "乌龙茶").confirmation_token or ""
        failed = _confirm(http, token)
        publisher.fail = False
        again = _confirm(http, token)
        fresh = _confirm(http, _preview(http, "乌龙茶").confirmation_token or "")

    assert (failed.status, again.status) == ("failed", "failed")
    assert again.request_id == failed.request_id
    assert fresh.request_id != failed.request_id
    assert fresh.status == "accepted"
    assert len(publisher.published) == 1


async def test_where_a_forget_got_to_is_readable(ledger) -> None:
    """The confirm usually answers ``accepted``; this is how a person learns more."""
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "乌龙茶")
    http, _ = _client(backend, ledger)
    with http:
        outcome = _confirm(http, _preview(http, "乌龙茶").confirmation_token or "")
        before = http.get(
            FORGET_STATUS_PATH, params={"request_id": outcome.request_id}, headers=AUTH
        )
        await ledger.record_applied(outcome.request_id, kind="privacy_mutation")
        after = http.get(
            FORGET_STATUS_PATH, params={"request_id": outcome.request_id}, headers=AUTH
        )

    assert OwnerForgetProgress.model_validate(before.json()).status == "accepted"
    assert OwnerForgetProgress.model_validate(after.json()).status == "applied"


def test_only_owner_forgets_are_readable(ledger) -> None:
    """The ledger also holds the Agent's commands, which are not a person's to read."""
    http, _ = _client(FakeMemoryBackend(), ledger)
    with http:
        foreign = http.get(FORGET_STATUS_PATH, params={"request_id": "turn-123"}, headers=AUTH)
        unknown = http.get(
            FORGET_STATUS_PATH, params={"request_id": "owner-forget-nope"}, headers=AUTH
        )

    assert foreign.status_code == 422
    assert unknown.status_code == 404


def test_a_confirm_without_a_token_is_refused(ledger) -> None:
    http, publisher = _client(FakeMemoryBackend(), ledger)
    with http:
        response = http.post(FORGET_CONFIRM_PATH, headers=AUTH)

    assert response.status_code == 422
    assert publisher.published == []


def test_a_forged_token_is_refused(ledger) -> None:
    http, publisher = _client(FakeMemoryBackend(), ledger)
    with http:
        response = http.post(
            FORGET_CONFIRM_PATH, params={"confirmation_token": "not-a-real-token"}, headers=AUTH
        )

    assert response.status_code == 409
    assert publisher.published == []


def test_a_token_minted_for_another_space_is_refused(ledger) -> None:
    signer = PrivacyConfirmationSigner()
    token, _proof = signer.issue(
        memory_space_id="realm_someone_else",
        action="delete",
        target="x",
        drawer_ids=["drawer_1"],
    )
    http, publisher = _client(FakeMemoryBackend(), ledger, signer=signer)
    with http:
        response = http.post(
            FORGET_CONFIRM_PATH, params={"confirmation_token": token}, headers=AUTH
        )

    assert response.status_code == 409
    assert publisher.published == []


def test_an_archive_token_is_not_an_owner_forget(ledger) -> None:
    """The steward path may archive; a person's confirm acts only on a person's preview."""
    signer = PrivacyConfirmationSigner()
    token, _proof = signer.issue(
        memory_space_id=SPACE, action="archive", target="x", drawer_ids=["drawer_1"]
    )
    http, publisher = _client(FakeMemoryBackend(), ledger, signer=signer)
    with http:
        response = http.post(
            FORGET_CONFIRM_PATH, params={"confirmation_token": token}, headers=AUTH
        )

    assert response.status_code == 409
    assert publisher.published == []


async def test_an_expired_preview_cannot_be_confirmed(ledger) -> None:
    """A decision made ten minutes ago is not a decision about now."""
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "乌龙茶")
    http, publisher = _client(backend, ledger, signer=PrivacyConfirmationSigner(ttl_seconds=1))
    with http:
        token = _preview(http, "乌龙茶").confirmation_token or ""
        time.sleep(1.1)
        response = http.post(
            FORGET_CONFIRM_PATH, params={"confirmation_token": token}, headers=AUTH
        )

    assert response.status_code == 409
    assert publisher.published == []


async def test_a_host_that_cannot_publish_does_not_offer_a_confirm(ledger) -> None:
    """Absent rather than always-failing: a button this Host could never honour."""
    backend = FakeMemoryBackend()
    await _canonical(backend, "drawer_1", "乌龙茶")
    http, _ = _client(backend, ledger, with_publisher=False)
    with http:
        preview = http.post(FORGET_PREVIEW_PATH, params={"target": "乌龙茶"}, headers=AUTH)
        confirm = http.post(
            FORGET_CONFIRM_PATH, params={"confirmation_token": "whatever"}, headers=AUTH
        )

    assert preview.status_code == 200
    assert confirm.status_code == 404, "the route is not mounted at all"
