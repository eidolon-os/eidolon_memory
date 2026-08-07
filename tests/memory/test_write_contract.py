"""The write half of the boundary, and the one rule it exists to hold.

``MemoryWriteContract`` was declared when the contracts package was written and
implemented by nothing until 2026-08-07. The three operations were all real — a
turn goes out over NATS, explicit writes and forgets go through MCP tools — but
scattered across two transports with no object gathering them, so the sentence
the contract is built around had nowhere to live:

    ``applied`` is the only status that means stored and readable. A caller that
    says "I'll remember that" on ``accepted`` is lying to the user.

Everything here is about that sentence staying true.
"""

from __future__ import annotations

from typing import Any

from eidolon_memory_contracts import (
    ConversationTurnPayload,
    MemoryActorContext,
    MemoryReadContract,
    MemoryWriteContract,
)

from eidolon.memory.application.memory_service import MemoryService

SPACE = "default.alice.default"


def _ctx() -> MemoryActorContext:
    return MemoryActorContext(
        memory_realm_id=SPACE,
        memory_space_id=SPACE,
        owner_id="alice",
        companion_id="default",
    )


def _turn(turn_id: str = "turn-1") -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id=turn_id,
        context=_ctx(),
        user_text="记住我对花生过敏",
        assistant_text="好的",
        timestamp="2026-08-07T00:00:00Z",
    )


class _Publisher:
    """A command bus that accepts everything and remembers what it saw."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[Any] = []
        self._fail = fail

    async def publish(self, command: Any) -> None:
        if self._fail:
            raise RuntimeError("bus unreachable")
        self.published.append(command)


class _Status:
    """A command-status ledger whose terminal answer the test chooses."""

    def __init__(self, terminal: dict[str, Any] | None) -> None:
        self._terminal = terminal
        self.accepted: list[str] = []
        self.failed: list[tuple[str, str]] = []

    async def record_accepted(self, request_id: str, *, kind: str) -> None:
        self.accepted.append(request_id)

    async def record_failed(self, request_id: str, *, kind: str, error: str) -> None:
        self.failed.append((request_id, error))

    async def wait_terminal(self, request_id: str, *, timeout_seconds: float) -> Any:
        if self._terminal is None:
            return None
        payload = {"request_id": request_id, **self._terminal}
        return type("Record", (), {"to_dict": lambda self, p=payload: p})()


def _service(**kwargs: Any) -> MemoryService:
    return MemoryService(router=None, settings=None, **kwargs)  # type: ignore[arg-type]


def test_one_object_satisfies_both_halves_of_the_boundary() -> None:
    """A ``runtime_checkable`` Protocol nothing passes is decoration, not a contract."""

    service = _service()
    assert isinstance(service, MemoryReadContract)
    assert isinstance(service, MemoryWriteContract)


# ── the rule ──────────────────────────────────────────────────────────────────


async def test_a_write_the_ledger_confirms_is_the_only_one_called_applied() -> None:
    publisher = _Publisher()
    service = _service(
        command_publisher=publisher,
        command_status=_Status({"status": "applied", "resource_id": "drawer_1"}),
    )

    outcome = await service.write_confirmed_fact(
        _ctx(), "我对花生过敏", source_event_id="evt-1", tool_call_id="call-1"
    )

    assert outcome.status == "applied"
    assert outcome.durable is True
    assert outcome.resource_id == "drawer_1"


async def test_a_write_that_outruns_the_wait_is_accepted_not_applied() -> None:
    """The timeout case, which is the one a caller is most likely to speak over."""

    service = _service(
        command_publisher=_Publisher(),
        command_status=_Status(None),  # never reaches a terminal state
    )

    outcome = await service.write_confirmed_fact(
        _ctx(),
        "我对花生过敏",
        source_event_id="evt-1",
        tool_call_id="call-1",
        wait_applied_seconds=0.01,
    )

    assert outcome.status == "accepted"
    assert outcome.durable is False, "accepted must never read as stored"


async def test_a_write_with_no_ledger_is_accepted_not_applied() -> None:
    """Durable on the bus, unobservable in outcome. Claiming more would be a guess."""

    service = _service(command_publisher=_Publisher(), command_status=None)

    outcome = await service.write_confirmed_fact(
        _ctx(), "我对花生过敏", source_event_id="evt-1", tool_call_id="call-1"
    )

    assert outcome.status == "accepted"
    assert outcome.durable is False


async def test_a_write_that_never_reached_the_bus_says_so() -> None:
    status = _Status(None)
    service = _service(command_publisher=_Publisher(fail=True), command_status=status)

    outcome = await service.write_confirmed_fact(
        _ctx(), "我对花生过敏", source_event_id="evt-1", tool_call_id="call-1"
    )

    assert outcome.status == "failed"
    assert "bus unreachable" in (outcome.error or "")
    # Recorded, so a later status lookup agrees with what the caller was told.
    assert status.failed and status.failed[0][0] == outcome.request_id


async def test_the_same_tool_call_writes_under_the_same_id_twice() -> None:
    """Retrying a write must be a retry, not a second memory."""

    publisher = _Publisher()
    service = _service(command_publisher=publisher, command_status=_Status(None))

    first = await service.write_confirmed_fact(
        _ctx(), "我对花生过敏", source_event_id="evt-1", tool_call_id="call-1"
    )
    second = await service.write_confirmed_fact(
        _ctx(), "我对花生过敏", source_event_id="evt-1", tool_call_id="call-1"
    )

    assert first.request_id == second.request_id == "call-1"


async def test_an_empty_fact_fails_rather_than_publishing_nothing() -> None:
    publisher = _Publisher()
    service = _service(command_publisher=publisher, command_status=_Status(None))

    outcome = await service.write_confirmed_fact(
        _ctx(), "   ", source_event_id="evt-1", tool_call_id="call-1"
    )

    assert outcome.status == "failed"
    assert publisher.published == []


# ── turns are the other kind of write, and say less on purpose ────────────────


async def test_a_published_turn_reports_the_bus_not_the_memory() -> None:
    class _Turns:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        async def publish_turn(self, payload: Any) -> None:
            self.seen.append(payload)

    turns = _Turns()
    receipt = await _service(turn_publisher=turns).publish_turn(_turn(), trace_id="t-1")

    assert receipt.state == "published"
    assert receipt.turn_id == "turn-1"
    assert receipt.subject and receipt.subject.startswith("eidolon.memory.turn.")
    assert receipt.trace_id == "t-1"
    assert len(turns.seen) == 1


async def test_no_bus_is_a_state_of_its_own_not_a_success() -> None:
    receipt = await _service().publish_turn(_turn())

    assert receipt.state == "skipped_no_bus"


async def test_a_turn_the_bus_refused_does_not_raise_into_the_caller() -> None:
    class _Broken:
        async def publish_turn(self, payload: Any) -> None:
            raise RuntimeError("no route to host")

    receipt = await _service(turn_publisher=_Broken()).publish_turn(_turn())

    assert receipt.state == "publish_failed"
    assert "no route to host" in (receipt.error or "")


# ── forgetting: the token has to survive the round trip ───────────────────────


async def test_a_preview_hands_back_a_token_its_own_confirm_accepts(tmp_path) -> None:
    """The read half was broken in a way only the write half could reveal.

    ``preview_forget`` set ``requires_explicit_confirmation`` and left
    ``confirmation_token`` empty, on the reasoning that minting was "a write
    concern". So a caller following the contract literally was told to hand back
    a token it had never been given. Nothing caught it because the MCP surface
    mints its own — the contract's own path was the broken one.

    This goes through ``preview_forget`` rather than calling the signer, because
    calling the signer is exactly what a caller cannot do and what made the gap
    invisible.
    """

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.domain.space_runtime import MemorySpaceRuntime, SpaceLedgers
    from eidolon.memory.domain.wire import MemoryWireRecord

    backend = FakeMemoryBackend()
    backend.docs[f"{SPACE}::drawer_peanut"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key="drawer_peanut",
        value="用户对花生过敏",
        metadata={"memory_space_id": SPACE, "wing": "Wing_Profile"},
    )

    class _Router:
        async def resolve(self, space_id: str) -> MemorySpaceRuntime:
            return MemorySpaceRuntime(
                space_id=space_id,
                backend=backend,
                palace_path=str(tmp_path),
                kg=None,
                ledgers=SpaceLedgers(),
            )

    publisher = _Publisher()
    service = MemoryService(
        _Router(),  # type: ignore[arg-type]
        load_memory_settings(),
        command_publisher=publisher,
        command_status=_Status({"status": "applied"}),
    )

    preview = await service.preview_forget(_ctx(), "忘掉用户对花生过敏", action="delete")

    assert preview.status == "preview"
    assert preview.requires_explicit_confirmation
    assert preview.confirmation_token, "the caller was told to hand back a token"
    assert preview.expires_at

    outcome = await service.confirm_forget(_ctx(), preview.confirmation_token)

    assert outcome.status == "applied"
    assert outcome.action == "delete"
    assert outcome.forgotten_ids == ["drawer_peanut"]
    assert publisher.published[0].drawer_ids == ["drawer_peanut"]


async def test_a_forged_token_fails_and_forgets_nothing() -> None:
    publisher = _Publisher()
    service = _service(command_publisher=publisher, command_status=_Status(None))

    outcome = await service.confirm_forget(_ctx(), "not-a-real-token")

    assert outcome.status == "failed"
    assert outcome.forgotten_ids == []
    assert publisher.published == [], "a bad token must never widen into a deletion"


async def test_a_token_for_another_space_is_refused() -> None:
    publisher = _Publisher()
    service = _service(command_publisher=publisher, command_status=_Status(None))
    token, _ = service.privacy_signer.issue(
        memory_space_id="default.bob.default",
        action="delete",
        target="花生",
        drawer_ids=["drawer_1"],
    )

    outcome = await service.confirm_forget(_ctx(), token)

    assert outcome.status == "failed"
    assert publisher.published == []


async def test_an_unconfirmed_forget_reports_no_forgotten_ids() -> None:
    """``accepted`` means the ids are *going* to go, not that they are gone."""

    service = _service(command_publisher=_Publisher(), command_status=_Status(None))
    token, _ = service.privacy_signer.issue(
        memory_space_id=SPACE, action="archive", target="花生", drawer_ids=["drawer_1"]
    )

    outcome = await service.confirm_forget(_ctx(), token, wait_applied_seconds=0.01)

    assert outcome.status == "accepted"
    assert outcome.forgotten_ids == []


def test_both_surfaces_verify_against_one_signer() -> None:
    """A proof carries a per-instance secret, so two signers cannot agree.

    The MCP privacy tools used to construct their own. With the service also
    minting tokens, a preview from one surface and a confirm on the other would
    meet on different keys — and fail as forged, only in a deployment where both
    surfaces are actually used.
    """

    import inspect

    from eidolon.memory.entrypoints import mcp_server

    source = inspect.getsource(mcp_server)
    assert "signer=service.privacy_signer" in source
    assert "signer = PrivacyConfirmationSigner()" not in source, (
        "the MCP surface is minting against a second key again"
    )
