"""Run with the adjacent Agent venv; all databases and broker are test-owned.

The Memory e2e fixture owns its real worker and broker. This client exercises
production Data HTTP serialization, SDK signatures, Agent admission/outbox and
Memory wire reads without importing Memory implementation into Agent.
"""

import asyncio
import json
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import grpc
import httpx
from eidolon_agent.app.transport.grpc.chat_servicer import EidolonAgentServicer
from eidolon_agent.app.transport.grpc.interceptors import AuthInterceptor
from eidolon_agent.app.transport.grpc.proto import pbg
from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.types.turn import TurnEvent, TurnStatus
from eidolon_agent.domain.agent.turn import TurnEngine
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.history import HistoryFanout
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer
from eidolon_agent.infra.events.nats_bus import NatsEventBus
from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort
from eidolon_agent.infra.persistence.memory_turn_dispatch import MemoryTurnDispatcher
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore
from eidolon_agent.infra.system_data import SystemDataCompanionRuntimeAuthority
from eidolon_data import DataSettings, DataStore
from eidolon_data.api.companion_authority import create_app
from eidolon_sdk.biz.person import PersonCreate
from eidolon_sdk.biz.runtime import RuntimeTokenVerifier, sign_runtime_token
from eidolon_sdk.biz.speaker_attestation import sign_observation
from eidolon_sdk.biz.speaker_identity import SpeakerIdentityObservation
from eidolon_sdk.biz.system_data import SystemDataRuntimeClient
from eidolon_sdk.biz.system_data.models import PersonMemoryDefaults

OWNER, PERSON, COMPANION = "journey-owner", "a" * 32, "journey-companion"
SECRET = "isolated-person-journey-signing-key"


async def main():
    phase, folder = sys.argv[1], Path(sys.argv[2])
    settings = DataSettings(
        sqlite_path=str(folder / "data.db"),
        object_store_path=str(folder / "objects"),
        audit_nats_url=None,
    )
    store = DataStore.open(settings)
    await store.init_schema()
    try:
        if phase == "seed":
            await store.owner_commands.create_owner(owner_id=OWNER)
            await store.companion_workspaces.provision_workspace(
                owner_id=OWNER,
                companion_id=COMPANION,
                genome_id="journey-genome",
                realm_id="journey-owner-realm",
            )
            defaults = await store.persons.memory_defaults(OWNER)
            await store.persons.memory_defaults(
                OWNER,
                defaults["owner_revision"],
                PersonMemoryDefaults(
                    candidate={"save_allowed": True, "session_recall_allowed": True}
                ),
            )
            await store.persons.create(
                OWNER,
                PersonCreate(
                    person_id=PERSON, display_name="candidate", maturity="candidate", is_named=False
                ),
            )
            print(json.dumps(await store.persons.memory_state(OWNER, PERSON)))
            return
        app = create_app(settings, service_token=SECRET, memory_roster_token=SECRET)
        bus = NatsEventBus(sys.argv[3])
        runtime = AgentRuntimeStore.open(folder / "agent.db")
        await runtime.init_schema()
        server = None
        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://isolated-data"
                ) as http,
            ):
                authorizer = RuntimeSessionAuthorizer(
                    SystemDataCompanionRuntimeAuthority(
                        SystemDataRuntimeClient(http, "http://isolated-data", service_token=SECRET)
                    )
                )
                token, _ = sign_runtime_token(
                    secret=SECRET,
                    owner_id=OWNER,
                    companion_id=COMPANION,
                    session_id="room",
                    source_session_id="room",
                    person_id=PERSON,
                    person_revision=1,
                    identity_epoch=2,
                    memory_mode="provisional",
                    ttl_seconds=60,
                )
                identity = await RuntimeTokenVerifier(secret=SECRET, allow_provisional=True).verify(
                    token
                )
                args = {
                    key: getattr(identity, key)
                    for key in (
                        "owner_id",
                        "companion_id",
                        "session_id",
                        "source_session_id",
                        "person_id",
                        "person_revision",
                        "identity_epoch",
                        "memory_mode",
                        "device_id",
                    )
                }
                scope = await authorizer.authorize(**args)
                received = asyncio.Queue()

                class CapturingAgent:
                    async def run_turn(self, value):
                        await received.put(value)
                        yield TurnEvent.delta(value.turn_id, 0, "accepted", 0.0)
                        yield TurnEvent.done(value.turn_id, 1, TurnStatus.OK, 0.0)

                registry = SimpleNamespace(
                    resolve_runtime=AsyncMock(
                        return_value=SimpleNamespace(
                            owner_id=OWNER,
                            companion_id=COMPANION,
                            genome_id=scope.runtime.genome_id,
                            agent=CapturingAgent(),
                        )
                    )
                )
                server = grpc.aio.server(
                    interceptors=[
                        AuthInterceptor(RuntimeTokenVerifier(secret=SECRET, allow_provisional=True))
                    ]
                )
                pbg.add_EidolonAgentServicer_to_server(
                    EidolonAgentServicer(
                        agent_registry=registry,
                        signals_bus=SimpleNamespace(recent=AsyncMock(return_value=[])),
                        proactive_bus=None,
                        runtime_sessions=authorizer,
                        speaker_secret=SECRET,
                    ),
                    server,
                )
                port_number = server.add_insecure_port("127.0.0.1:0")
                assert port_number
                await server.start()
                channel = Path(__file__).resolve().parents[5] / "eidolon_channel"
                channel_script = Path(__file__).with_name("provisional_channel_client.py")

                async def turn(trace, text, *, observed=True, signed_text=None):
                    now = time.time()
                    observation = SpeakerIdentityObservation(
                        owner_id=OWNER,
                        session_id="room",
                        turn_id=trace,
                        person_id=PERSON,
                        person_revision=1,
                        maturity="candidate",
                        status="identified",
                        confidence="controlled_trial",
                        reason="voice_match",
                        profile_id="v",
                        profile_revision=1,
                        model_id="m",
                        weights_hash="a" * 64,
                        preprocessing_version="v",
                        scoring_version="v",
                        decision_version="trial",
                        issued_at=now,
                        expires_at=now + 10,
                        source=dict(
                            request_id=trace,
                            session_id="room",
                            input_id="mic",
                            generation=1,
                            sample_rate=16000,
                            start_sample=0,
                            end_sample=32000,
                        ),
                    )
                    observation_token = (
                        sign_observation(
                            observation,
                            secret=SECRET,
                            text=text if signed_text is None else signed_text,
                        )
                        if observed
                        else None
                    )
                    process = await asyncio.create_subprocess_exec(
                        str(channel / ".venv/bin/python"),
                        str(channel_script),
                        cwd=channel,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            process.communicate(
                                json.dumps(
                                    dict(
                                        target=f"127.0.0.1:{port_number}",
                                        runtime_token=token,
                                        observation_token=observation_token,
                                        trace=trace,
                                        text=text,
                                    )
                                ).encode()
                            ),
                            timeout=20,
                        )
                    except BaseException:
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
                        raise
                    assert process.returncode == 0, stderr.decode() + stdout.decode()
                    value = await asyncio.wait_for(received.get(), timeout=2)
                    assert value.context.trace_id == trace and value.text == text
                    return value

                first = await turn("first", "我暂时喜欢青色")
                assert first.context.provisional_memory_save and not first.context.memory_enabled
                engine = object.__new__(TurnEngine)
                engine._genome_id = scope.runtime.genome_id
                engine._fanout = HistoryFanout(memory_outbox=runtime.memory_turn_outbox)
                await engine._publish_memory_observation(first, datetime.now(UTC))
                assert await runtime.memory_turn_outbox.pending_count() == 1
                assert await MemoryTurnDispatcher(runtime, bus).dispatch_once() == 1
                assert await runtime.memory_turn_outbox.pending_count() == 0
                pool = SimpleNamespace(
                    session_for=AsyncMock(side_effect=AssertionError("broad MCP forbidden"))
                )
                port = EidolonMemoryPort(pool=pool, event_bus=bus)
                compiler = ContextCompiler(
                    personas_service=SimpleNamespace(
                        realize_context=AsyncMock(
                            return_value=SimpleNamespace(
                                system_prompt="test persona", debug_trace=()
                            )
                        )
                    ),
                    instance_locator=lambda *_: (COMPANION, scope.runtime.genome_id),
                    history_manager=SimpleNamespace(recent_window=AsyncMock(return_value=[])),
                    memory_port=port,
                )
                second = await turn("second", "刚才说的颜色是什么")
                async with asyncio.timeout(10):
                    while True:
                        result = await compiler._memory_recall(second, timeout_s=0.2)
                        if result and result[0]:
                            break
                        await asyncio.sleep(0.05)
                assert "我暂时喜欢青色" in result[0] and "UNCONFIRMED SOURCES" in result[0]
                assert not result[1]
                compiled = await compiler.compile(second)
                assert "我暂时喜欢青色" in compiled[0].content
                assert "UNCONFIRMED SOURCES" in compiled[0].content
                other_session = replace(
                    second,
                    session_id="another-room",
                    context=replace(second.context, source_session_id="another-room"),
                )
                foreign = await compiler._memory_recall(other_session, timeout_s=0.2)
                assert foreign[0] is None and not foreign[1]
                unknown = await turn("unknown", "谁的颜色", observed=False)
                assert not unknown.context.memory_write_enabled
                assert await compiler._memory_recall(unknown, timeout_s=0.2) is None
                mismatched = await turn("mismatched", "另一个请求", signed_text="原签名文本")
                assert mismatched.context.person_id is None
                assert not mismatched.context.memory_write_enabled
                assert await compiler._memory_recall(mismatched, timeout_s=0.2) is None
                assert mismatched.metadata.get("speaker_observation_rejected") is True
                latencies = []
                for _ in range(20):
                    start = time.perf_counter()
                    assert (await compiler._memory_recall(second, timeout_s=0.2))[0]
                    latencies.append((time.perf_counter() - start) * 1000)
                pool.session_for.assert_not_awaited()
                await store.persons.suspend_memory(OWNER, PERSON, 1)
                try:
                    await authorizer.authorize(**args)
                except NotFoundError:
                    pass
                else:
                    raise AssertionError("revoked Person remained authorized")
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "read_count": 20,
                            "channel_grpc": True,
                            "invalid_observation_rejected": True,
                            "warm_read_p95_ms": sorted(latencies)[18],
                            "warm_read_max_ms": max(latencies),
                        }
                    )
                )
        finally:
            if server is not None:
                await server.stop(0)
            await bus.close()
            await runtime.close()
    finally:
        await store.close()


asyncio.run(main())
