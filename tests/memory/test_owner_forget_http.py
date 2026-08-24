"""Two steps to forget something, and what each step refuses.

The dangerous shape here is a one-step delete driven by a topic: a person types
"忘了上周那件事", something matches, and memory changes. The two steps exist so
that what is confirmed is *what was seen* — the preview binds the exact drawer
ids into a signed token, and the confirm acts on the token rather than
re-resolving the topic.

Everything below is a way that binding could be loosened without looking wrong:

- confirming without a token, or with one from another space, or an expired one;
- re-resolving on confirm, so the set acted on is whatever the words match now;
- issuing a token when nothing matched, so a button deletes nothing and says it
  worked;
- offering a partial set when the match was too broad, so the rest is believed
  kept when it was merely unseen.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.application.forget import ForgetResolutionLimitExceeded
from eidolon.memory.application.privacy_confirmation import PrivacyConfirmationSigner
from eidolon.memory.entrypoints import owner_memory_http
from eidolon.memory.entrypoints.owner_memory_http import (
    FORGET_CONFIRM_PATH,
    FORGET_PREVIEW_PATH,
    owner_memory_routes,
)

TOKEN = "memory-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPACE = "realm_owner_one"


class _Candidate:
    def __init__(self, key: str, *, score: float = 1.0) -> None:
        self.key = key
        self.score = score

    def to_dict(self) -> dict[str, Any]:
        return {"drawer_id": self.key, "score": self.score, "preview": "上周那件事"}


class _Runtime:
    backend = object()
    palace_path = "/tmp/palace"


class _Service:
    def __init__(self, *, signer: PrivacyConfirmationSigner) -> None:
        self.privacy_signer = signer
        self.contexts: list[Any] = []

    async def runtime_for(self, context: Any) -> _Runtime:
        self.contexts.append(context)
        return _Runtime()


class _Publisher:
    def __init__(self) -> None:
        self.published: list[Any] = []


class _Settings:
    wings: list[Any] = []


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates: list[_Candidate] | None = None,
    too_broad: bool = False,
    signer: PrivacyConfirmationSigner | None = None,
    with_publisher: bool = True,
    applied: dict[str, Any] | None = None,
):
    resolved: list[str] = []

    async def _find(_backend, _space, target, **_kwargs):
        resolved.append(target)
        if too_broad:
            raise ForgetResolutionLimitExceeded("too many matches")
        return list(candidates or [])

    published: list[Any] = []

    async def _publish(_publisher, _status, command, *, wait_seconds):
        published.append(command)
        return applied if applied is not None else {"status": "applied"}

    monkeypatch.setattr(owner_memory_http, "find_forget_candidates", _find)
    monkeypatch.setattr(owner_memory_http, "publish_with_status", _publish)

    service = _Service(signer=signer or PrivacyConfirmationSigner())
    app = Starlette(
        routes=owner_memory_routes(
            service=service,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
            memory_space_id=SPACE,
            owner_id="owner-1",
            service_token=TOKEN,
            command_publisher=_Publisher() if with_publisher else None,
            command_status=None,
        )
    )
    return TestClient(app), service, resolved, published


def test_a_preview_shows_the_exact_set_and_binds_it(monkeypatch) -> None:
    http, _service, resolved, published = _client(
        monkeypatch, candidates=[_Candidate("drawer_1"), _Candidate("drawer_2")]
    )
    with http:
        body = http.post(
            f"{FORGET_PREVIEW_PATH}?target=上周那件事", headers=AUTH
        ).json()

    assert body["status"] == "preview"
    assert [entry["drawer_id"] for entry in body["entries"]] == ["drawer_1", "drawer_2"]
    assert body["confirmation_token"]
    assert resolved == ["上周那件事"]
    # A preview changes nothing. That is the whole difference from a delete.
    assert published == []


def test_more_than_one_match_asks_again_before_deleting(monkeypatch) -> None:
    """A guess must not be read as an instruction."""
    http, _service, _resolved, _published = _client(
        monkeypatch, candidates=[_Candidate("drawer_1"), _Candidate("drawer_2")]
    )
    with http:
        body = http.post(f"{FORGET_PREVIEW_PATH}?target=那件事", headers=AUTH).json()

    assert body["needs_confirmation"] is True


def test_one_exact_match_does_not(monkeypatch) -> None:
    http, _service, _resolved, _published = _client(
        monkeypatch, candidates=[_Candidate("drawer_1", score=1.0)]
    )
    with http:
        body = http.post(f"{FORGET_PREVIEW_PATH}?target=乌龙茶", headers=AUTH).json()

    assert body["needs_confirmation"] is False


def test_nothing_matched_issues_no_token(monkeypatch) -> None:
    """Otherwise a person presses a button that removes nothing and reports success."""
    http, _service, _resolved, _published = _client(monkeypatch, candidates=[])
    with http:
        body = http.post(f"{FORGET_PREVIEW_PATH}?target=没有的事", headers=AUTH).json()

    assert body["status"] == "not_found"
    assert "confirmation_token" not in body


def test_too_broad_offers_nothing_rather_than_a_partial_set(monkeypatch) -> None:
    """The one outcome worse than refusing.

    A partial set would leave the rest believed kept when it was merely unseen,
    and the person would have no way to know which half they got.
    """
    http, _service, _resolved, _published = _client(monkeypatch, too_broad=True)
    with http:
        body = http.post(f"{FORGET_PREVIEW_PATH}?target=一切", headers=AUTH).json()

    assert body["status"] == "too_broad"
    assert "confirmation_token" not in body


def test_a_target_is_required(monkeypatch) -> None:
    http, _service, resolved, _published = _client(monkeypatch)
    with http:
        blank = http.post(FORGET_PREVIEW_PATH, headers=AUTH)
        spaces = http.post(f"{FORGET_PREVIEW_PATH}?target=%20%20", headers=AUTH)

    assert (blank.status_code, spaces.status_code) == (422, 422)
    assert resolved == [], "nothing was named, so nothing was resolved"


def test_an_action_outside_the_contract_is_refused(monkeypatch) -> None:
    http, _service, _resolved, _published = _client(monkeypatch)
    with http:
        response = http.post(
            f"{FORGET_PREVIEW_PATH}?target=x&action=obliterate", headers=AUTH
        )

    assert response.status_code == 422


def test_confirming_applies_exactly_what_the_preview_bound(monkeypatch) -> None:
    """Not the topic. The token carries the ids; the confirm never re-resolves.

    Between the two steps the words may match something else — a new turn, a
    steward write — and acting on that would delete what the person never saw.
    """
    http, _service, resolved, published = _client(
        monkeypatch, candidates=[_Candidate("drawer_1"), _Candidate("drawer_2")]
    )
    with http:
        preview = http.post(
            f"{FORGET_PREVIEW_PATH}?target=上周那件事", headers=AUTH
        ).json()
        confirmed = http.post(
            f"{FORGET_CONFIRM_PATH}?confirmation_token={preview['confirmation_token']}",
            headers=AUTH,
        ).json()

    assert confirmed["entry_count"] == 2
    assert published[0].drawer_ids == ["drawer_1", "drawer_2"]
    assert published[0].target == "上周那件事"
    # Resolved once, at preview time, and never again.
    assert resolved == ["上周那件事"]


def test_a_person_asking_is_not_recorded_as_the_eidolon_deciding(monkeypatch) -> None:
    """The MCP tool records ``agent`` because the Eidolon asked.

    This surface is reached by the Host's own Admin on a person's behalf, so it
    records ``admin``. Saying ``agent`` here would put someone's decision in
    their Eidolon's name in the ledger they might one day read.
    """
    http, _service, _resolved, published = _client(
        monkeypatch, candidates=[_Candidate("drawer_1")]
    )
    with http:
        preview = http.post(f"{FORGET_PREVIEW_PATH}?target=乌龙茶", headers=AUTH).json()
        http.post(
            f"{FORGET_CONFIRM_PATH}?confirmation_token={preview['confirmation_token']}",
            headers=AUTH,
        )

    assert published[0].issuer == "admin"


def test_a_confirm_without_a_token_is_refused(monkeypatch) -> None:
    http, _service, _resolved, published = _client(monkeypatch)
    with http:
        response = http.post(FORGET_CONFIRM_PATH, headers=AUTH)

    assert response.status_code == 422
    assert published == []


def test_a_forged_token_is_refused(monkeypatch) -> None:
    http, _service, _resolved, published = _client(monkeypatch)
    with http:
        response = http.post(
            f"{FORGET_CONFIRM_PATH}?confirmation_token=not-a-real-token", headers=AUTH
        )

    assert response.status_code == 409
    assert published == []


def test_a_token_minted_for_another_space_is_refused(monkeypatch) -> None:
    """One person's confirmation must not act on another's memory."""
    signer = PrivacyConfirmationSigner()
    token, _proof = signer.issue(
        memory_space_id="realm_someone_else",
        action="delete",
        target="x",
        drawer_ids=["drawer_1"],
    )

    http, _service, _resolved, published = _client(monkeypatch, signer=signer)
    with http:
        response = http.post(
            f"{FORGET_CONFIRM_PATH}?confirmation_token={token}", headers=AUTH
        )

    assert response.status_code == 409
    assert published == []


def test_an_expired_preview_cannot_be_confirmed(monkeypatch) -> None:
    """A decision made ten minutes ago is not a decision about now.

    The proof is process-local and short-lived on purpose: a restart or a long
    pause invalidates the preview, and never memory itself.
    """
    signer = PrivacyConfirmationSigner(ttl_seconds=1)
    http, _service, _resolved, published = _client(
        monkeypatch, candidates=[_Candidate("drawer_1")], signer=signer
    )
    with http:
        preview = http.post(f"{FORGET_PREVIEW_PATH}?target=乌龙茶", headers=AUTH).json()
        token = preview["confirmation_token"]

        import time

        time.sleep(1.1)
        response = http.post(
            f"{FORGET_CONFIRM_PATH}?confirmation_token={token}", headers=AUTH
        )

    assert response.status_code == 409
    assert published == []


def test_the_applied_status_is_relayed_rather_than_assumed(monkeypatch) -> None:
    """Publishing is durable; applying is a projection that may still be running.

    Reporting "done" for a command that has only been accepted would be the
    comfortable lie. The status the ledger records is passed through as-is.
    """
    http, _service, _resolved, _published = _client(
        monkeypatch,
        candidates=[_Candidate("drawer_1")],
        applied={"status": "accepted", "request_id": "r1"},
    )
    with http:
        preview = http.post(f"{FORGET_PREVIEW_PATH}?target=乌龙茶", headers=AUTH).json()
        body = http.post(
            f"{FORGET_CONFIRM_PATH}?confirmation_token={preview['confirmation_token']}",
            headers=AUTH,
        ).json()

    assert body["status"] == "accepted"
    assert body["request_id"] == "r1"


def test_a_host_that_cannot_publish_does_not_offer_a_confirm(monkeypatch) -> None:
    """Absent rather than always-failing.

    A confirm route that could never succeed is a button this Host promises and
    cannot honour. The preview stays useful for seeing what would go.
    """
    http, _service, _resolved, _published = _client(monkeypatch, with_publisher=False)
    with http:
        preview = http.post(f"{FORGET_PREVIEW_PATH}?target=x", headers=AUTH)
        confirm = http.post(
            f"{FORGET_CONFIRM_PATH}?confirmation_token=whatever", headers=AUTH
        )

    assert preview.status_code == 200
    # 404: the path does not exist on this Host, which is what "absent" means.
    assert confirm.status_code == 404, "the route is not mounted at all"
