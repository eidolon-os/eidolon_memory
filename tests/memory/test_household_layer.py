"""The household layer (design §3.6, A2).

The Owner's default Realm is the household's: every speaker's household facts
go there and every speaker recalls them. A Person's Realm keeps that person's
facts. The steward classifies each claim; each Realm keeps its own layer; and
the household Realm recalls only what was classified household, so what it
held from before the layer existed stays unrecalled until it is classified.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_memory_contracts import (
    ConversationTurnPayload,
    MemoryActorContext,
    envelope_memory_payload,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.application.public_recall import search_all_wings_mcp_style
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.config.memory_settings import MemorySettings, load_memory_settings
from eidolon.memory.config.users import UserEntry
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.kg import KgInvalidationAction, KgTripleAction
from eidolon.memory.domain.space_runtime import MemorySpaceRuntime, SpaceLedgers
from eidolon.memory.domain.steward import EntityMention, PrivacyAction, StewardDecision
from eidolon.memory.entrypoints.supervisor import _agent_cli_argv
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.dlq import DlqLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger

HOUSEHOLD = "r_household"
PERSON = "rp_person"


def _fragment(content: str, *, layer: str, space: str = HOUSEHOLD) -> MemoryFragment:
    return MemoryFragment(
        memory_space_id=space,
        source_turn_id="turn-1",
        wing="Wing_Life",
        # The fake store keys records by room.
        room=f"life_{layer}",
        content=content,
        memory_type="life",
        importance=4,
        confidence=0.9,
        layer=layer,
    )


def _decision(space: str, *, privacy: bool = True) -> StewardDecision:
    return StewardDecision(
        should_write=True,
        reason="t",
        fragments=[
            _fragment("家里每周二晚上收垃圾", layer="household", space=space),
            _fragment("说话人最近睡不好", layer="personal", space=space),
        ],
        triples=[KgTripleAction(subject="self", predicate="likes", object="茶")],
        invalidations=[
            KgInvalidationAction(
                subject="self", predicate="likes", object="咖啡", evidence_quote="不喜欢咖啡了"
            )
        ],
        mentions=[EntityMention(entity_id="self", alias="我")],
        privacy_actions=[
            PrivacyAction(
                action="archive_topic", target="体检", reason="t", evidence_quote="别再提体检"
            )
        ]
        if privacy
        else [],
    )


def _turn(space: str, *, person: str | None, speaker: str | None = None) -> ConversationTurnPayload:
    identity = {"person_id": person, "identity_epoch": 1} if person else {}
    return ConversationTurnPayload(
        turn_id="turn-1",
        context=MemoryActorContext(
            memory_realm_id=space,
            owner_id="owner-1",
            companion_id="companion-1",
            device_id="box-3",
            session_id="room-1",
            **identity,
        ),
        timestamp="2026-11-04T10:00:00Z",
        user_text="垃圾每周二晚上收，我最近睡不好",
        assistant_text="",
        metadata={"speaker_person_id": speaker} if speaker else {},
    )


def _msg(turn: ConversationTurnPayload) -> SimpleNamespace:
    envelope = envelope_memory_payload(turn.model_dump(mode="json"), kind="conversation_turn")
    return SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode(),
        ack=AsyncMock(),
        nak=AsyncMock(),
        metadata=SimpleNamespace(num_delivered=1),
    )


def test_a_person_realm_keeps_the_personal_layer_and_everything_structured() -> None:
    kept = _decision(PERSON).for_layer("personal")

    assert [f.content for f in kept.fragments] == ["说话人最近睡不好"]
    assert kept.triples and kept.invalidations and kept.mentions and kept.privacy_actions
    assert kept.should_write


def test_the_household_realm_keeps_household_text_and_the_turns_privacy_actions() -> None:
    kept = _decision(HOUSEHOLD).for_layer("household")

    assert [f.content for f in kept.fragments] == ["家里每周二晚上收垃圾"]
    assert (kept.triples, kept.invalidations, kept.mentions) == ([], [], [])
    assert [a.target for a in kept.privacy_actions] == ["体检"]


def test_a_realm_with_nothing_of_its_layer_writes_nothing() -> None:
    decision = _decision(PERSON).model_copy(
        update={"fragments": [_fragment("说话人最近睡不好", layer="personal")]}
    )

    assert not decision.for_layer("household").should_write


def test_unclear_is_personal() -> None:
    assert MemoryFragment.model_fields["layer"].default == "personal"


def _steward():
    import yaml

    from eidolon.memory.application.steward.llm import LiteLLMSteward

    config = Path(__file__).resolve().parents[2] / "config/settings.example.yaml"
    settings = MemorySettings.model_validate(yaml.safe_load(config.read_text(encoding="utf-8")))
    return LiteLLMSteward(settings)


def test_a_household_claim_is_kept_as_text_even_with_a_graph_fact() -> None:
    claim = {
        "wing": "Wing_Life",
        "room": "life_home",
        "content": "家里每周二晚上收垃圾",
        "evidence_quote": "垃圾每周二晚上收",
        "layer": "household",
        "memory_type": "life",
        "importance": 4,
        "confidence": 0.9,
        "fact": {"subject": "self", "predicate": "does", "object": "收垃圾"},
    }
    raw = json.dumps({"should_write": True, "reason": "t", "claims": [claim]}, ensure_ascii=False)

    decision = _steward()._parse_decision(
        raw, context=_turn(HOUSEHOLD, person=None).context, source_turn_id="turn-1"
    )

    assert decision.triples == []
    assert [(f.content, f.layer) for f in decision.fragments] == [
        ("家里每周二晚上收垃圾", "household")
    ]


def test_the_steward_is_asked_for_a_layer() -> None:
    steward = _steward()
    prompt = steward._render_user_prompt(_turn(HOUSEHOLD, person=None))

    assert "layer" in prompt and "personal/household" in prompt


async def _process(tmp_path: Path, turn: ConversationTurnPayload, *, layer: str):
    steward = SimpleNamespace(
        extraction_version="test:v1",
        # A privacy action keeps a turn's facts out, so none here.
        decide=AsyncMock(return_value=_decision(turn.context.memory_space_id, privacy=False)),
    )
    backend = LockedBackend(FakeMemoryBackend())
    dlq = DlqLedger(tmp_path / "dlq.sqlite3", space_id=turn.context.memory_space_id)
    msg = _msg(turn)
    await process_turn_message(
        msg,
        steward=steward,
        backend=backend,
        settings=load_memory_settings(),
        max_deliveries=3,
        expected_memory_space_id=turn.context.memory_space_id,
        realm_layer=layer,
        dlq_writer=dlq,
        decision_store=ExtractionDecisionLedger(tmp_path / "decisions.sqlite3"),
        canonical_facts=CanonicalFactLedger(tmp_path / "canonical.sqlite3"),
    )
    return steward, backend.inner, msg, dlq


async def test_the_household_realm_stores_household_facts_with_who_said_them(tmp_path) -> None:
    _, backend, msg, _ = await _process(
        tmp_path, _turn(HOUSEHOLD, person=None, speaker="person-a"), layer="household"
    )

    msg.ack.assert_awaited_once()
    [drawer] = backend.docs.values()
    assert drawer.metadata["layer"] == "household"
    assert drawer.metadata["speaker_person_id"] == "person-a"
    # No identity: every speaker in the household may recall it.
    assert drawer.metadata.get("person_id") in (None, "")


async def test_an_unrecognised_speakers_household_fact_has_no_speaker(tmp_path) -> None:
    _, backend, _, _ = await _process(tmp_path, _turn(HOUSEHOLD, person=None), layer="household")

    [drawer] = backend.docs.values()
    assert drawer.metadata["speaker_person_id"] == ""


async def test_a_person_realm_stores_only_that_persons_facts(tmp_path) -> None:
    _, backend, msg, _ = await _process(
        tmp_path, _turn(PERSON, person="person-a"), layer="personal"
    )

    msg.ack.assert_awaited_once()
    assert [d.metadata["layer"] for d in backend.docs.values()] == ["personal"]


@pytest.mark.parametrize(
    ("space", "person", "layer"),
    [(HOUSEHOLD, "person-a", "household"), (PERSON, None, "personal")],
)
async def test_a_turn_for_the_other_layer_is_refused_before_extraction(
    tmp_path, space, person, layer
) -> None:
    steward, backend, msg, dlq = await _process(tmp_path, _turn(space, person=person), layer=layer)

    steward.decide.assert_not_awaited()
    msg.ack.assert_awaited_once()
    assert backend.docs == {}
    assert len(await dlq.list()) == 1


async def _household_backend() -> FakeMemoryBackend:
    backend = FakeMemoryBackend()
    ctx = _turn(HOUSEHOLD, person=None).context
    for content, layer in (("收垃圾 household", "household"), ("收垃圾 personal", "personal")):
        await backend.ingest_fragment(
            _fragment(content, layer=layer).model_copy(
                update={"audience": "companion:companion-1", "owner_id": ctx.owner_id}
            )
        )
    # Written before the layer existed: no ``layer`` at all.
    await backend.ingest_text(
        wing="Wing_Life",
        room="life_unclassified",
        text="收垃圾 unclassified",
        metadata={"memory_space_id": HOUSEHOLD, "audience": "companion:companion-1"},
    )
    return backend


async def test_household_recall_returns_only_household_facts() -> None:
    backend = await _household_backend()
    ctx = _turn(HOUSEHOLD, person=None).context

    records = await search_all_wings_mcp_style(
        backend,
        load_memory_settings(),
        query="收垃圾",
        context=ctx,
        top_k=5,
        wing=None,
        room=None,
        layers=("household",),
    )

    assert [r.value for r in records] == ["收垃圾 household"]


class _Router:
    def __init__(self, backend: FakeMemoryBackend) -> None:
        self.runtime = MemorySpaceRuntime(
            space_id=HOUSEHOLD,
            backend=backend,
            palace_path="/tmp/household",
            kg=None,
            ledgers=SpaceLedgers(),
        )

    def serves(self, space_id: str) -> bool:
        return space_id == HOUSEHOLD

    async def resolve(self, space_id: str) -> MemorySpaceRuntime:
        return self.runtime

    def held_spaces(self) -> list[str]:
        return [HOUSEHOLD]

    async def aclose(self) -> None:
        return None


async def test_the_household_realm_recalls_no_promises_and_no_graph() -> None:
    service = MemoryService(
        _Router(await _household_backend()),
        MemorySettings.model_validate({"kg": {"backend": "none"}}),
        household_spaces=frozenset({HOUSEHOLD}),
    )
    ctx = _turn(HOUSEHOLD, person=None).context

    assert service.recalled_layers(ctx) == ("household",)
    commitments = await service.read_active_commitments(ctx)
    assert (commitments.commitments, commitments.degraded) == ([], False)
    found = await service.search(ctx, "收垃圾")
    assert [s.text for s in found.snippets] == ["收垃圾 household"]


def test_a_person_realm_recalls_every_layer() -> None:
    service = MemoryService(
        _Router(FakeMemoryBackend()),
        MemorySettings.model_validate({"kg": {"backend": "none"}}),
        household_spaces=frozenset({HOUSEHOLD}),
    )

    assert service.recalled_layers(_turn(PERSON, person="person-a").context) is None


@pytest.mark.parametrize("scope", ["owner", "person"])
def test_the_supervisor_tells_each_worker_whose_memory_it_serves(scope) -> None:
    user = UserEntry(
        id=HOUSEHOLD if scope == "owner" else PERSON,
        scope=scope,
        person_id="person-a" if scope == "person" else None,
        owner_id="owner-1",
        port=9001,
    )

    argv = _agent_cli_argv(user, palace_path=Path("/tmp/p"))

    assert argv[argv.index("--realm-scope") + 1] == scope

