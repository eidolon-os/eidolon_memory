import asyncio
from unittest.mock import MagicMock

from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.application.livekit_recall import LiveKitRecallService
from eidolon.memory.application.steward.common import stamp_fragment_identity
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.fragments import MemoryFragment


def actor():
    return MemoryActorContext(
        memory_realm_id="realm-a", person_id="a", identity_epoch=1, session_id="s", companion_id="c"
    )


def test_steward_cannot_forge_person_provenance():
    fragment = MemoryFragment(
        memory_space_id="wrong",
        source_turn_id="t",
        wing="w",
        room="r",
        content="tea",
        memory_type="fact",
        importance=1,
        confidence=1,
        person_id="forged",
        identity_epoch=99,
    )
    stamped = stamp_fragment_identity(fragment, context=actor())
    assert (stamped.memory_space_id, stamped.person_id, stamped.identity_epoch) == (
        "realm-a",
        "a",
        1,
    )
    legacy = stamp_fragment_identity(
        fragment, context=MemoryActorContext(memory_realm_id="r", companion_id="c")
    )
    assert legacy.person_id is None and legacy.identity_epoch is None


async def test_delayed_recall_keeps_original_identity(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def query(*args, **kwargs):
        entered.set()
        await release.wait()
        assert kwargs["context"].person_id == "a"
        return {"vector": [], "kg": []}

    monkeypatch.setattr("eidolon.memory.application.livekit_recall.recall_with_kg_fusion", query)
    service = LiveKitRecallService(MagicMock(), MemorySettings(), palace_path="unused")
    ctx = actor()
    pending = asyncio.create_task(service.recall_context_with_records("tea", context=ctx))
    await entered.wait()
    ctx.person_id = "b"
    ctx.identity_epoch = 2
    release.set()
    result = await pending
    assert result["identity_binding"]["person_id"] == "a"
    assert result["identity_binding"]["identity_epoch"] == 1
    assert result["identity_binding"] != ctx.identity_binding()


def test_legacy_extraction_hash_survives_contract_upgrade():
    import hashlib
    import json

    from eidolon_memory_contracts import ConversationTurnPayload

    from eidolon.memory.domain.extraction_decision import extraction_input_hash

    turn = ConversationTurnPayload(
        turn_id="t",
        context=MemoryActorContext(memory_realm_id="r"),
        user_text="tea",
        assistant_text="ok",
        timestamp="2026-10-27T00:00:00Z",
    )
    old = turn.model_dump(mode="json")
    old["context"].pop("person_id")
    old["context"].pop("identity_epoch")
    expected = hashlib.sha256(
        json.dumps(old, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert extraction_input_hash(turn) == expected
    bound = turn.model_copy(update={"context": actor()})
    assert extraction_input_hash(bound) != expected
