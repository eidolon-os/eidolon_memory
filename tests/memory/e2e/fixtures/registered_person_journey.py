"""G3 for registered people: real Data, Agent and Memory agree on who spoke (design §4.1).

Run with the adjacent Agent venv; every database and the broker are test-owned.
Recognition is injected, never run: each turn carries a signed observation, and
an attribution is the turn verdict recognition would post to Data. The Memory
e2e fixture owns two real workers, the Person's Realm and the household Realm.

One room, one registered Person (阿伟), four turns:

1. 阿伟, recognised on the turn;
2. 阿伟, committed while recognition ran and too late for the 150 ms bound, so
   unrecognised; the turn verdict reaches Data afterwards;
3. 阿伟, recognised again, asking about turn 2;
4. someone not recognised.

Each turn checks the chains that must agree: the history label, the Realm
written, the Realm recalled, how the speaker is addressed; and that the
household layer is read and written whoever speaks.
"""

import asyncio
import base64
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from eidolon_agent.app.interaction import AcceptReply, ReplyRequest
from eidolon_agent.config.settings import MemoryEndpoint, NatsSettings
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.memory import MemoryQueryPlan
from eidolon_agent.domain.agent.companion import CompanionAgent
from eidolon_agent.domain.agent.turn import TurnEngine
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.domain.history import HistoryFanout, HistoryManager
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.infra.events import InMemoryEventBus
from eidolon_agent.infra.events.nats_bus import NatsEventBus
from eidolon_agent.infra.llm import LLMRouter
from eidolon_agent.infra.memory.discovery import MemoryRoutingTable
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort
from eidolon_agent.infra.persistence import build_agent_history_hydrator, build_agent_turn_persister
from eidolon_agent.infra.persistence.agent_runtime import build_agent_conversation_guard
from eidolon_agent.infra.persistence.memory_turn_dispatch import MemoryTurnDispatcher
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore, MemoryTurnOutboxRow
from eidolon_agent.infra.persistence.voice_attributions import VoiceAttributionFollower
from eidolon_agent.infra.system_data import SystemDataCompanionRuntimeAuthority
from eidolon_data import DataSettings, DataStore
from eidolon_data.api.companion_authority import create_app
from eidolon_sdk.biz.dialogue_control import CommittedTurnDecision, TurnCommitBoundary
from eidolon_sdk.biz.person import PersonCreate
from eidolon_sdk.biz.runtime import RuntimeTokenVerifier, sign_runtime_token
from eidolon_sdk.biz.speaker_attestation import sign_observation
from eidolon_sdk.biz.speaker_identity import (
    IdentityDecisions,
    PersonEvidence,
    SpeakerIdentityObservation,
)
from eidolon_sdk.biz.system_data import SystemDataRuntimeClient
from eidolon_sdk.biz.voice_attribution import turn_verdict
from eidolon_sdk.biz.voiceprint import (
    EnrollmentCompletion,
    EnrollmentCreate,
    EnrollmentEvaluation,
    ExtractedSample,
    SpeakerAudioQuality,
    VoiceAudioRequest,
    enrollment_evidence_digest,
)
from eidolon_sdk.core.runtime import BackgroundTaskRunner
from sqlalchemy import select

OWNER, COMPANION, GENOME = "g3-owner", "g3-companion", "g3-genome"
PERSON, NAME = "b" * 32, "阿伟"
HOUSEHOLD = "g3-household-realm"
SECRET = "isolated-g3-journey-signing-key-0123456789"
ROOM, CONVERSATION, BOX = "room", "room-session:room", "esp-box-3"
MODEL = dict(model_id="test", weights_hash="a" * 64, preprocessing_version="v1")
DATA = "http://isolated-data"


def _sample(n):
    return ExtractedSample(**MODEL, vector=[1.0, n * 0.01], speech_ms=4500,
                           quality={"clipping": 0.0})


async def seed(store):
    await store.owner_commands.create_owner(owner_id=OWNER)
    await store.companion_workspaces.provision_workspace(
        owner_id=OWNER, companion_id=COMPANION, genome_id=GENOME, realm_id=HOUSEHOLD)
    await store.persons.create(OWNER, PersonCreate(person_id=PERSON, display_name=NAME))
    # Registered as the phone does it: three samples, then a completed enrollment.
    voiceprints = store.voiceprints
    enrollment = await voiceprints.create_enrollment(
        OWNER, EnrollmentCreate(person_id=PERSON, operation_id="enroll", display_name=NAME))
    for i in range(3):
        await voiceprints.add_sample(OWNER, enrollment.enrollment_id, str(i), _sample(i))
    recording = VoiceAudioRequest(pcm_base64=base64.b64encode(b"\x01\x00" * 32000).decode())
    samples = (await voiceprints.evidence(OWNER, enrollment.enrollment_id)).samples
    await voiceprints.complete(OWNER, enrollment.enrollment_id, EnrollmentCompletion(
        evaluation=EnrollmentEvaluation(
            evidence_digest=enrollment_evidence_digest(samples),
            recording_sha256=hashlib.sha256(base64.b64decode(recording.pcm_base64)).hexdigest(),
            template=next(iter(samples.values())), trial=_sample(3)),
        recording=recording))
    state = await store.persons.memory_state(OWNER, PERSON)
    assert state["state"] == "active", state
    return {"person_realm": state["realm_id"], "household_realm": HOUSEHOLD}


def observation(trace, *, recognised, pending=False):
    """What Channel signs for one utterance: 阿伟 by every use, or nobody."""
    now = time.time()
    if pending:
        return SpeakerIdentityObservation(
            owner_id=OWNER, session_id=ROOM, turn_id=trace, status="unavailable",
            reason="recognition_pending", issued_at=now, expires_at=now + 10)
    posterior = 0.999 if recognised else 0.30
    return SpeakerIdentityObservation(
        owner_id=OWNER, session_id=ROOM, turn_id=trace, status="compared", reason="compared",
        **MODEL, backend_version="b", policy_version="g3",
        evidence=[PersonEvidence(person_id=PERSON, person_revision=1, maturity="established",
                                 is_named=True, profile_id="v", profile_revision=1,
                                 llr=8.0 if recognised else -1.0, posterior=posterior)],
        posterior_unknown=0.0005 if recognised else 0.6,
        condition="far_same",
        decisions=(IdentityDecisions(personal_memory=PERSON, speaker_label=PERSON,
                                     addressing=PERSON)
                   if recognised else IdentityDecisions()),
        display_name=NAME if recognised else None,
        audio_quality=SpeakerAudioQuality(audio_ms=3000, required_speech_ms=500, speech_ms=2400),
        track_id=f"track-{trace}",
        issued_at=now, expires_at=now + 10,
        source=dict(request_id=trace, session_id=ROOM, input_id="mic", generation=1,
                    sample_rate=16000, start_sample=0, end_sample=48000, channel=BOX),
    )


class RecordedRecall:
    """The real port, noting which Realm each recall read."""

    def __init__(self, port):
        self._port, self.realms = port, []

    async def recall_context(self, *args, **kwargs):
        self.realms.append(kwargs["memory_realm_id"])
        return await self._port.recall_context(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._port, name)


class Model:
    model_id = "g3-model"

    def __init__(self):
        self.prompts = []

    async def stream(self, messages, **_kwargs):
        self.prompts.append(messages)
        yield LLMDelta(text_delta="好的。")
        yield LLMDelta(finish=LLMFinishReason.STOP)


async def verify(folder, nats_url, realms, workers):
    person_realm, household = realms["person_realm"], realms["household_realm"]
    settings = DataSettings(sqlite_path=str(folder / "data.db"),
                            object_store_path=str(folder / "objects"), audit_nats_url=None)
    app = create_app(settings, service_token=SECRET, memory_roster_token=SECRET)
    bus = NatsEventBus(nats_url)
    runtime = AgentRuntimeStore.open(folder / "agent.db")
    await runtime.init_schema()
    pool = None
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=DATA) as http,
        ):
            data = SystemDataRuntimeClient(http, DATA, service_token=SECRET)
            authority = SystemDataCompanionRuntimeAuthority(data)
            authorizer = RuntimeSessionAuthorizer(authority)
            history = HistoryManager(hydrate_messages=build_agent_history_hydrator(runtime))
            routes = MemoryRoutingTable.from_static(endpoints=[
                MemoryEndpoint(memory_space_id=realm, mcp_url=urls[0], ops_mcp_url=urls[1])
                for realm, urls in workers.items()], nats=NatsSettings(url=nats_url))
            fanout = HistoryFanout(memory_routes=routes, memory_outbox=runtime.memory_turn_outbox)
            pool = McpClientPool(routes=routes)
            memory = EidolonMemoryPort(pool=pool, event_bus=bus)
            recalls = RecordedRecall(memory)
            model = Model()
            personas = SimpleNamespace(realize_context=AsyncMock(
                return_value=SimpleNamespace(system_prompt="你是家里的伙伴。", debug_trace=())))
            # Which Realm is read is the question here, not how fast: the
            # deadlines leave room for a cold MCP session.
            compiler = ContextCompiler(
                personas_service=personas, instance_locator=lambda *_: (COMPANION, GENOME),
                history_manager=history, memory_port=recalls, memory_timeout_s=5.0,
                context_read_timeout_s=5.0, active_commitment_timeout_s=5.0)
            events = InMemoryEventBus()
            engine = TurnEngine(
                compiler=compiler, llm=LLMRouter(providers={"fake": model}, default="fake"),
                tool_dispatcher=ToolDispatcher(ToolRegistry()), history=history, fanout=fanout,
                input_guardrail=InputGuardrail(), output_guardrail=OutputGuardrail(),
                crisis=CrisisHandler(event_bus=events), event_bus=events,
                personas_service=personas, genome_id=GENOME,
                conversation_guard=build_agent_conversation_guard(runtime),
                turn_persister=build_agent_turn_persister(runtime, model_id_provider=lambda: "g3"),
                background_tasks=BackgroundTaskRunner(component="g3"))
            agent = CompanionAgent(companion_id=COMPANION, turn_engine=engine)
            registry = SimpleNamespace(resolve_runtime=AsyncMock(return_value=SimpleNamespace(
                owner_id=OWNER, companion_id=COMPANION, genome_id=GENOME, agent=agent)))
            accept = AcceptReply(registry, speaker_secret=SECRET)
            dispatcher = MemoryTurnDispatcher(runtime, bus)
            follower = VoiceAttributionFollower(
                runtime, read_feed=data.voice_attributions,
                resolve_realm=authority.resolve_person_memory, fanout=fanout, history=history)
            # The room's credential, as Channel installs it once 阿伟 has been
            # recognised: each turn's own observation still decides each use.
            token, _ = sign_runtime_token(
                secret=SECRET, owner_id=OWNER, companion_id=COMPANION, session_id=ROOM,
                source_session_id=ROOM, person_id=PERSON, person_revision=1, identity_epoch=1,
                ttl_seconds=300)
            claims = await RuntimeTokenVerifier(secret=SECRET).verify(token)
            scope = await authorizer.authorize(**{key: getattr(claims, key) for key in (
                "owner_id", "companion_id", "session_id", "source_session_id", "person_id",
                "person_revision", "identity_epoch", "memory_mode", "device_id")})
            assert scope.household_realm_id == household

            async def written(turn_id):
                """The Realms this turn is queued for; a delivered row leaves the outbox."""
                async with runtime.read_session_factory() as session:
                    rows = (await session.scalars(select(MemoryTurnOutboxRow).where(
                        MemoryTurnOutboxRow.turn_id == turn_id))).all()
                return sorted(row.delivery_id.split("@", 1)[1] for row in rows)

            async def deliver():
                while await runtime.memory_turn_outbox.pending_count():
                    assert await dispatcher.dispatch_once()

            async def turn(trace, text, observed):
                before = len(recalls.realms)
                prepared = await accept.prepare(scope, ReplyRequest(
                    conversation_id=CONVERSATION, text=text, input_modality="voice",
                    turn_id=f"agent-{trace}", trace_id=trace,
                    metadata={
                        "speaker_observation_token": sign_observation(
                            observed, secret=SECRET, text=text),
                        # Channel commits a voice turn before it is evidence for memory.
                        "turn_decision": CommittedTurnDecision.create(
                            text=text, boundary=TurnCommitBoundary.FRAMEWORK_COMPLETED,
                            eot_score=0.8).as_metadata(),
                    }))
                finished = [e async for e in prepared.agent.run_turn(prepared.turn)][-1]
                assert finished.kind.value == "done" and finished.data["status"] == "ok", finished
                system = model.prompts[-1][0].content
                queued = await written(prepared.turn.turn_id)
                await deliver()
                return SimpleNamespace(turn=prepared.turn, system=system,
                                       read=sorted(recalls.realms[before:]), written=queued)

            async def stored(realm, text, *, person=None):
                plan = MemoryQueryPlan(semantic_query=text, episodic_query=text)
                async with asyncio.timeout(30):
                    while True:
                        found = await memory.recall_context(
                            OWNER, text, memory_realm_id=realm, plan=plan, timeout_s=2,
                            companion_id=COMPANION, person_id=person,
                            identity_epoch=1 if person else None, session_id=ROOM)
                        if text in (found.context or ""):
                            return found.context
                        await asyncio.sleep(0.1)

            async def held(realm, text, *, person=None):
                plan = MemoryQueryPlan(semantic_query=text, episodic_query=text)
                found = await memory.recall_context(
                    OWNER, text, memory_realm_id=realm, plan=plan, timeout_s=2,
                    companion_id=COMPANION, person_id=person,
                    identity_epoch=1 if person else None, session_id=ROOM)
                return text in (found.context or "")

            async def post_verdict(observed):
                snapshot = (await http.get(
                    f"/api/companion-authority/v1/owners/{OWNER}/voiceprints/recognition-candidates",
                    params={"channel": BOX, "session_id": ROOM},
                    headers={"Authorization": f"Bearer {SECRET}"})).json()
                verdict = turn_verdict(observed, snapshot_revision=snapshot["revision"])
                response = await http.post(
                    f"/api/companion-authority/v1/owners/{OWNER}/voiceprints/verdicts",
                    content=verdict.model_dump_json(),
                    headers={"Authorization": f"Bearer {SECRET}",
                             "Content-Type": "application/json"})
                assert response.status_code == 200, response.text

            report = {}

            # 1. 阿伟, recognised on the turn: every chain says 阿伟.
            heard = observation("t1", recognised=True)
            first = await turn("t1", "我对花生过敏", heard)
            assert first.turn.context.memory_enabled and first.turn.context.person_id == PERSON
            assert "当前说话人：阿伟" in first.system
            assert first.read == sorted([person_realm, household]), first.read
            assert first.written == sorted([person_realm, household]), (
                first.written, first.turn.metadata.get("memory_write_trace"),
                first.turn.metadata.get("household_write_trace"))
            await stored(person_realm, "我对花生过敏", person=PERSON)
            await stored(household, "我对花生过敏")
            # Channel records the turn's own attribution as well (design §3.5).
            await post_verdict(heard)

            # 2. Committed while recognition ran; the result misses the bound.
            second = await turn("t2", "我的生日是五月三号", observation("t2", recognised=False,
                                                                      pending=True))
            assert second.turn.metadata["late_speaker_observation"]["state"] == "timeout"
            assert not second.turn.context.memory_enabled and second.turn.context.person_id is None
            assert "当前说话人：未识别" in second.system and "当前说话人：阿伟" not in second.system
            assert "当前说话人没认出" in second.system
            assert "阿伟：我对花生过敏" in second.system
            assert "[个人记忆] 本轮未读取（身份未确认）" in second.system
            assert second.read == [household], second.read
            assert second.written == [household], second.written
            assert second.turn.metadata["memory_write_trace"]["attributable"] is True
            await stored(household, "我的生日是五月三号")
            assert not await held(person_realm, "我的生日是五月三号", person=PERSON)
            # Recognition finishes; its verdict reaches Data after the reply.
            await post_verdict(observation("t2", recognised=True))
            assert await follower.apply_owner(OWNER) == 2
            # Turn 2 goes to 阿伟's Realm now; turn 1, written there on the spot, is not
            # queued again.
            assert await written(second.turn.turn_id) == [person_realm]
            assert await written(first.turn.turn_id) == []
            await deliver()
            await stored(person_realm, "我的生日是五月三号", person=PERSON)
            # The label survives a restart: it is read back from the Agent's store.
            restored = await HistoryManager(
                hydrate_messages=build_agent_history_hydrator(runtime)
            ).recent_window(conversation_id=CONVERSATION, window=10)
            sources = {m.metadata.get("turn_id"): m.metadata.get("speaker_source")
                       for m in restored if m.role.value == "user"}
            assert sources[second.turn.turn_id]["person_id"] == PERSON, sources
            assert sources[second.turn.turn_id]["reason"] == "voice_attribution"

            # 3. 阿伟 again: turn 2 is now his in history and in his memory.
            third = await turn("t3", "我的生日是哪天？", observation("t3", recognised=True))
            assert "当前说话人：阿伟" in third.system
            assert "阿伟：我对花生过敏" in third.system
            assert "阿伟：我的生日是五月三号" in third.system
            assert "只有标为「阿伟」的原话" in third.system
            # Block headers, not the harness line that names them.
            mine = third.system[third.system.index("[你的记忆]\n"):]
            assert "我的生日是五月三号" in mine.split("[家里的记忆]\n")[0], third.system
            assert third.read == sorted([person_realm, household]), third.read
            assert third.written == sorted([person_realm, household]), third.written

            # 4. Someone not recognised: no one's personal memory, the household still.
            fourth = await turn("t4", "家里的医药箱在哪？", observation("t4", recognised=False))
            assert not fourth.turn.context.memory_enabled
            assert "当前说话人：未识别" in fourth.system and "当前说话人：阿伟" not in fourth.system
            assert "阿伟：我的生日是五月三号" in fourth.system
            assert "[你的记忆]\n" not in fourth.system and "[家里的记忆]\n" in fourth.system
            assert fourth.turn.metadata["household_memory_trace"]["context_injected"] is True
            assert fourth.read == [household], fourth.read
            assert fourth.written == [household], fourth.written
            await stored(household, "家里的医药箱在哪？")
            assert not await held(person_realm, "家里的医药箱在哪？", person=PERSON)

            for step in (first, second, third, fourth):
                report[step.turn.context.trace_id] = {"read": step.read, "written": step.written}
            print(json.dumps({"passed": True, "turns": report}, ensure_ascii=False))
    finally:
        if pool is not None:
            await pool.close_all()
        await bus.close()
        await runtime.close()


async def main():
    phase, folder = sys.argv[1], Path(sys.argv[2])
    if phase == "seed":
        settings = DataSettings(sqlite_path=str(folder / "data.db"),
                                object_store_path=str(folder / "objects"), audit_nats_url=None)
        store = DataStore.open(settings)
        await store.init_schema()
        try:
            print(json.dumps(await seed(store)))
        finally:
            await store.close()
        return
    await verify(folder, sys.argv[3], json.loads(sys.argv[4]), json.loads(sys.argv[5]))


asyncio.run(main())
