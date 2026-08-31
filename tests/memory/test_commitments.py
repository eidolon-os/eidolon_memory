from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from eidolon_memory_contracts import MemoryIntent, MemoryIntentCommand

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.explicit_intents import apply_explicit_intent
from eidolon.memory.application.forget import (
    find_forget_candidates,
    forget_commitment_projections,
)
from eidolon.memory.application.steward.common import apply_privacy_actions
from eidolon.memory.domain.commitment import CommitmentConflict
from eidolon.memory.domain.extraction_decision import ExtractionDecisionRecord
from eidolon.memory.domain.steward import PrivacyAction, StewardDecision
from eidolon.memory.infrastructure.commitments import CommitmentLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger

SPACE = "r:alice:default"


def _intent(
    intent_id: str,
    *,
    operation: str = "add",
    status: str | None = None,
    target_id: str | None = None,
    participants: list[str] | None = None,
    action: str = "带 companion:default 去常州中华恐龙园",
    due_at: str | None = None,
) -> MemoryIntent:
    attributes = {
        "beneficiaries": ["companion:default"],
        "participants": participants or [],
        "condition": "有实体后",
    }
    if status is not None:
        attributes["status"] = status
    if due_at is not None:
        attributes["due_at"] = due_at
    return MemoryIntent(
        intent_id=intent_id,
        memory_space_id=SPACE,
        source_event_id=f"turn:{intent_id}",
        authority="explicit_user",
        intent_type="commitment",
        raw_claim="以后带你去常州中华恐龙园",
        operation_hint=operation,
        target_id=target_id,
        subject="self",
        predicate="promised",
        object=action,
        confidence=1.0,
        attributes=attributes,
    )


@pytest.mark.asyncio
async def test_active_commitments_prioritize_due_time_before_recent_updates(
    tmp_path,
) -> None:
    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")
    await ledger.apply(
        _intent(
            "intent:undated",
            operation="confirm",
            action="无期限承诺",
        )
    )
    await ledger.apply(
        _intent(
            "intent:later",
            operation="confirm",
            action="较晚承诺",
            due_at="2026-07-20T02:00:00Z",
        )
    )
    await ledger.apply(
        _intent(
            "intent:earliest",
            operation="confirm",
            action="最早承诺",
            due_at="2026-07-19T20:00:00Z",
        )
    )
    await ledger.apply(
        _intent(
            "intent:middle",
            operation="confirm",
            action="中间承诺",
            due_at="2026-07-20T09:00:00+08:00",
        )
    )

    current = await ledger.list_current(SPACE)
    bounded = await ledger.list_current(SPACE, limit=2)
    page = await ledger.list_current_page(SPACE, limit=2)

    assert [row.action for row in current] == [
        "最早承诺",
        "中间承诺",
        "较晚承诺",
        "无期限承诺",
    ]
    assert [row.action for row in bounded] == ["最早承诺", "中间承诺"]
    assert [row.action for row in page.commitments] == ["最早承诺", "中间承诺"]
    assert page.total == 4
    assert page.limit == 2
    assert page.truncated is True


def _command(intent: MemoryIntent) -> MemoryIntentCommand:
    return MemoryIntentCommand(
        request_id=f"request:{intent.intent_id}",
        memory_space_id=SPACE,
        issued_at="2026-07-17T00:00:00Z",
        issuer="agent",
        intent=intent,
    )


class _CommitmentKG:
    def __init__(self) -> None:
        self.rows: set[tuple[str, str, str]] = set()
        self.add_calls = 0
        self.invalidate_calls = 0

    async def query_entity(self, subject: str, **_kwargs):
        from types import SimpleNamespace

        return [
            SimpleNamespace(subject=s, predicate=p, object=o)
            for s, p, o in self.rows
            if s == subject
        ]

    async def match_entities_for_query(self, *_args, **_kwargs):
        return []

    async def add_triple(self, **kwargs):
        self.add_calls += 1
        self.rows.add((kwargs["subject"], kwargs["predicate"], kwargs["object"]))
        return "triple:commitment"

    async def invalidate(self, **kwargs):
        key = (kwargs["subject"], kwargs["predicate"], kwargs["object"])
        if key not in self.rows:
            return 0
        self.invalidate_calls += 1
        self.rows.remove(key)
        return 1

    async def forget_source_turns(self, source_turn_ids, *, hard=False):
        changed = len(self.rows)
        self.rows.clear()
        return changed


@pytest.mark.asyncio
async def test_commitment_supplement_updates_one_identity_and_keeps_history(
    tmp_path,
) -> None:
    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")

    proposed = await ledger.apply(_intent("intent:propose"))
    confirmed = await ledger.apply(
        _intent(
            "intent:confirm",
            operation="confirm",
            target_id=proposed.commitment.commitment_id,
        )
    )
    supplemented = await ledger.apply(
        _intent(
            "intent:supplement",
            operation="update",
            target_id=proposed.commitment.commitment_id,
            participants=["friend:小明", "friend:小红"],
        )
    )

    current = await ledger.list_current(SPACE)
    history = await ledger.history(SPACE, proposed.commitment.commitment_id)
    assert len(current) == 1
    assert current[0].status == "confirmed"
    assert current[0].participants == ["friend:小明", "friend:小红"]
    assert current[0].revision == 3
    assert [item.status for item in history] == [
        "proposed",
        "confirmed",
        "confirmed",
    ]
    assert confirmed.commitment.commitment_id == supplemented.commitment.commitment_id


@pytest.mark.asyncio
async def test_target_update_inherits_omitted_beneficiaries(tmp_path) -> None:
    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")
    created = await ledger.apply(_intent("intent:create", operation="confirm"))
    update = _intent(
        "intent:update",
        operation="update",
        target_id=created.commitment.commitment_id,
        participants=["friend:小明"],
    )
    update = update.model_copy(
        update={
            "attributes": {
                key: value
                for key, value in update.attributes.items()
                if key != "beneficiaries"
            }
        }
    )

    updated = await ledger.apply(update)

    assert updated.commitment.beneficiaries == ["companion:default"]
    assert updated.revision.snapshot.beneficiaries == ["companion:default"]


@pytest.mark.asyncio
async def test_commitment_terminal_state_is_replay_safe(tmp_path) -> None:
    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")
    created = await ledger.apply(_intent("intent:create", operation="confirm"))
    fulfilled_intent = _intent(
        "intent:fulfilled",
        operation="update",
        status="fulfilled",
        target_id=created.commitment.commitment_id,
    )

    fulfilled = await ledger.apply(fulfilled_intent)
    replay = await ledger.apply(fulfilled_intent)

    assert fulfilled.commitment.status == "fulfilled"
    assert fulfilled.revision_created is True
    assert replay.revision_created is False
    assert replay.commitment.revision == 2
    assert await ledger.list_current(SPACE) == []
    assert len(await ledger.list_current(SPACE, include_terminal=True)) == 1


@pytest.mark.asyncio
async def test_commitment_rejects_terminal_reopen_and_identity_change(tmp_path) -> None:
    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")
    created = await ledger.apply(_intent("intent:create", operation="confirm"))
    await ledger.apply(
        _intent(
            "intent:cancel",
            operation="invalidate",
            target_id=created.commitment.commitment_id,
        )
    )

    with pytest.raises(CommitmentConflict, match="invalid commitment transition"):
        await ledger.apply(
            _intent(
                "intent:reopen",
                operation="confirm",
                target_id=created.commitment.commitment_id,
            )
        )

    changed = _intent(
        "intent:changed",
        operation="update",
        target_id=created.commitment.commitment_id,
    ).model_copy(update={"object": "去另一个地方"})
    with pytest.raises(CommitmentConflict, match="identity fields"):
        await ledger.apply(changed)


@pytest.mark.asyncio
async def test_commitment_intent_reuse_with_different_payload_fails(tmp_path) -> None:
    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")
    original = _intent("intent:shared")
    await ledger.apply(original)

    with pytest.raises(CommitmentConflict, match="intent id reused"):
        await ledger.apply(
            original.model_copy(update={"raw_claim": "不同的承诺原话"})
        )


@pytest.mark.asyncio
async def test_explicit_commitment_projects_only_current_revision(tmp_path) -> None:
    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")
    backend = LockedBackend(FakeMemoryBackend())
    kg = _CommitmentKG()
    created_intent = _intent("intent:create", operation="confirm")

    created_resource = await apply_explicit_intent(
        backend,
        kg,
        _command(created_intent),
        commitments=ledger,
    )
    commitment_id = created_resource.split(":revision:", 1)[0]
    supplemented_intent = _intent(
        "intent:supplement",
        operation="update",
        target_id=commitment_id,
        participants=["friend:小明"],
    )
    await apply_explicit_intent(
        backend,
        kg,
        _command(supplemented_intent),
        commitments=ledger,
    )

    records = await backend.get_all(SPACE)
    assert len(records) == 2
    assert sum(row.metadata.get("privacy") == "do_not_recall" for row in records) == 1
    assert sum(row.metadata.get("privacy") == "normal" for row in records) == 1
    assert kg.add_calls == 1
    current = await ledger.get(SPACE, commitment_id)
    assert current is not None
    assert current.participants == ["friend:小明"]
    assert current.drawer_projection_state == "projected"
    assert current.kg_projection_state == "projected"

    fulfilled = _intent(
        "intent:fulfilled",
        operation="update",
        status="fulfilled",
        target_id=commitment_id,
    )
    await apply_explicit_intent(
        backend,
        kg,
        _command(fulfilled),
        commitments=ledger,
    )
    await apply_explicit_intent(
        backend,
        kg,
        _command(fulfilled),
        commitments=ledger,
    )

    assert await ledger.list_current(SPACE) == []
    assert kg.invalidate_calls == 1
    records = await backend.get_all(SPACE)
    assert all(row.metadata.get("privacy") == "do_not_recall" for row in records)


@pytest.mark.asyncio
async def test_commitment_delete_is_ledger_first_and_replay_safe(tmp_path) -> None:
    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")
    backend = LockedBackend(FakeMemoryBackend())
    kg = _CommitmentKG()
    intent = _intent("intent:privacy", operation="confirm", action="去海边看日出 canary")
    resource = await apply_explicit_intent(
        backend, kg, _command(intent), commitments=ledger
    )
    commitment_id = resource.split(":revision:", 1)[0]

    candidates = await find_forget_candidates(
        backend,
        SPACE,
        "去海边看日出 canary",
        commitments=ledger,
    )
    assert [(row.key, row.text) for row in candidates] == [
        (commitment_id, "去海边看日出 canary")
    ]

    drawers, statements = await forget_commitment_projections(
        backend,
        kg,
        ledger,
        SPACE,
        [commitment_id],
        hard=True,
        decision_store=ExtractionDecisionLedger(tmp_path / "decisions.sqlite3"),
    )

    assert len(drawers) == 1
    assert statements == 1
    assert await backend.get_all(SPACE) == []
    assert await ledger.get(SPACE, commitment_id) is None
    assert await ledger.history(SPACE, commitment_id) == []
    assert await ledger.list_current(SPACE, include_terminal=True) == []
    assert await ledger.list_for_privacy(SPACE, limit=10, offset=0) == []
    with pytest.raises(CommitmentConflict, match="forgotten commitment"):
        await ledger.apply(intent)
    with pytest.raises(CommitmentConflict, match="forgotten commitment"):
        await ledger.apply(
            _intent(
                "intent:privacy-replay",
                operation="confirm",
                action="去海边看日出 canary",
            )
        )


@pytest.mark.asyncio
async def test_spoken_privacy_action_deletes_commitment_through_the_same_ledger(
    tmp_path,
) -> None:
    commitments = CommitmentLedger(tmp_path / "commitments.sqlite3")
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    backend = LockedBackend(FakeMemoryBackend())
    kg = _CommitmentKG()
    intent = _intent(
        "intent:spoken-privacy",
        operation="confirm",
        action="去莫干山看萤火虫 canary",
    )
    resource = await apply_explicit_intent(
        backend,
        kg,
        _command(intent),
        commitments=commitments,
    )
    commitment_id = resource.split(":revision:", 1)[0]
    await decisions.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id=SPACE,
            source_turn_id=intent.source_event_id,
            extractor_version="test:v1",
            input_hash="spoken-commitment",
            decision=StewardDecision(should_write=True, reason="commitment"),
            intents=[intent],
        )
    )

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="去莫干山看萤火虫 canary",
                reason="user request",
            )
        ],
        audiences=("owner",),
        kg=kg,
        commitments=commitments,
        decision_store=decisions,
    )

    assert result.deleted_keys == [commitment_id]
    assert await commitments.get(SPACE, commitment_id) is None
    redacted = await decisions.get(SPACE, intent.source_event_id, "test:v1")
    assert redacted is not None and redacted.redacted is True


@pytest.mark.asyncio
async def test_commitment_forget_preview_binds_ledger_id_not_projection(tmp_path) -> None:
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger

    commitments = CommitmentLedger(tmp_path / "commitments.sqlite3")
    backend = LockedBackend(FakeMemoryBackend())
    kg = _CommitmentKG()
    intent = _intent("intent:mcp-privacy", operation="confirm", action="去玄武湖散步 canary")
    resource = await apply_explicit_intent(
        backend, kg, _command(intent), commitments=commitments
    )
    commitment_id = resource.split(":revision:", 1)[0]
    publisher = AsyncMock()
    status = CommandStatusLedger(
        tmp_path / "command-status.sqlite3", space_id=SPACE
    )
    mcp = build_control_plane_mcp(
        backend,
        load_memory_settings(),
        memory_space_id=SPACE,
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9997,
        kg=kg,
        command_publisher=publisher,
        command_status=status,
        commitments=commitments,
    )
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    preview = await tools["eidolon_memory_forget_preview"].fn(
        target="去玄武湖散步 canary", action="delete"
    )

    assert preview["status"] == "preview"
    assert preview["candidates"] == [
        {
            "id": commitment_id,
            "text": "去玄武湖散步 canary",
            "wing": "Wing_Future",
            "score": 1.0,
            "commitment_id": commitment_id,
            "kind": "commitment",
        }
    ]

    async def _record_applied(command):
        await status.record_applied(
            command.request_id,
            kind=command.kind,
            resource_id=f"delete:1:{command.preview_id}",
        )

    publisher.publish.side_effect = _record_applied
    confirmed = await tools["eidolon_memory_forget_confirm"].fn(
        confirmation_token=preview["confirmation_token"],
        wait_applied_seconds=0.1,
    )

    assert confirmed["status"] == "applied"
    command = publisher.publish.await_args.args[0]
    assert command.drawer_ids == []
    assert command.commitment_ids == [commitment_id]


@pytest.mark.asyncio
async def test_archived_commitment_can_be_previewed_then_hard_deleted(tmp_path) -> None:
    commitments = CommitmentLedger(tmp_path / "commitments.sqlite3")
    backend = LockedBackend(FakeMemoryBackend())
    kg = _CommitmentKG()
    intent = _intent(
        "intent:archive-privacy",
        operation="confirm",
        action="去紫金山看日落 canary",
    )
    resource = await apply_explicit_intent(
        backend, kg, _command(intent), commitments=commitments
    )
    commitment_id = resource.split(":revision:", 1)[0]

    await forget_commitment_projections(
        backend, kg, commitments, SPACE, [commitment_id], hard=False
    )

    assert await commitments.get(SPACE, commitment_id) is None
    archived = await find_forget_candidates(
        backend,
        SPACE,
        "去紫金山看日落 canary",
        commitments=commitments,
    )
    assert [row.key for row in archived] == [commitment_id]

    await forget_commitment_projections(
        backend,
        kg,
        commitments,
        SPACE,
        [commitment_id],
        hard=True,
        decision_store=ExtractionDecisionLedger(tmp_path / "decisions.sqlite3"),
    )

    assert await backend.get_all(SPACE) == []
    assert await commitments.list_for_privacy(SPACE, limit=10, offset=0) == []


@pytest.mark.asyncio
async def test_commitment_mcp_reads_are_realm_bound(tmp_path) -> None:
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp

    ledger = CommitmentLedger(tmp_path / "commitments.sqlite3")
    created = await ledger.apply(_intent("intent:mcp", operation="confirm"))
    mcp = build_control_plane_mcp(
        FakeMemoryBackend(),
        load_memory_settings(),
        memory_space_id=SPACE,
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9999,
        commitments=ledger,
    )
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    current = await tools["eidolon_memory_commitments"].fn()
    history = await tools["eidolon_memory_commitment_history"].fn(
        commitment_id=created.commitment.commitment_id,
    )

    assert [row["commitment_id"] for row in current["commitments"]] == [
        created.commitment.commitment_id
    ]
    assert history["status"] == "ok"
    assert [row["intent_id"] for row in history["revisions"]] == ["intent:mcp"]

    other_realm_mcp = build_control_plane_mcp(
        FakeMemoryBackend(),
        load_memory_settings(),
        memory_space_id="r:bob:default",
        palace_path=str(tmp_path),
        host="127.0.0.1",
        port=9998,
        commitments=ledger,
    )
    other_tools = {
        tool.name: tool for tool in other_realm_mcp._tool_manager.list_tools()
    }
    other_history = await other_tools["eidolon_memory_commitment_history"].fn(
        commitment_id=created.commitment.commitment_id,
    )

    assert other_history["status"] == "not_found"
    assert other_history["revisions"] == []
