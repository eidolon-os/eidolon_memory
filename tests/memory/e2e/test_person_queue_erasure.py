"""Real JetStream test: erasing one Person cannot replay or erase another."""

import asyncio
import shutil
import socket
import subprocess

import nats
import pytest
from eidolon_memory_contracts import (
    conversation_turn_subject,
    memory_command_subject,
    memory_sync_subject,
)
from nats.js.errors import NotFoundError

from eidolon.memory.infrastructure.nats.names import memory_consumer_name
from eidolon.memory.infrastructure.realm_erasure import purge_realm_messages


@pytest.mark.asyncio
async def test_erasure_purges_only_target_realms_messages_and_consumers(tmp_path):
    binary = shutil.which("nats-server")
    if not binary:
        pytest.skip("nats-server is required for real JetStream integration")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "nats.log").open("wb") as log:
        server = subprocess.Popen(
            [binary, "-a", "127.0.0.1", "-p", str(port), "-js", "-sd", str(tmp_path / "nats")],
            stdout=log,
            stderr=log,
        )
    nc = None
    try:
        url = f"nats://127.0.0.1:{port}"
        for _ in range(50):
            try:
                nc = await nats.connect(url, max_reconnect_attempts=0, connect_timeout=0.1)
                break
            except Exception:
                await asyncio.sleep(0.05)
        assert nc is not None
        js = nc.jetstream()
        await js.add_stream(name="TEST_PERSON_MEMORY", subjects=["eidolon.memory.>"])
        for realm in ("r_a", "r_b"):
            for role, subject in [
                ("turn", conversation_turn_subject(realm)),
                ("cmd", memory_command_subject(realm)),
                ("sync", memory_sync_subject(realm)),
            ]:
                await js.publish(subject, b"test-only")
                await js.pull_subscribe(
                    subject,
                    durable=memory_consumer_name("test", realm, role=role),
                    stream="TEST_PERSON_MEMORY",
                )
        await purge_realm_messages(url, "TEST_PERSON_MEMORY", "test", ["r_a"])
        await purge_realm_messages(url, "TEST_PERSON_MEMORY", "test", ["r_a"])
        for make in (conversation_turn_subject, memory_command_subject, memory_sync_subject):
            with pytest.raises(NotFoundError):
                await js.get_last_msg("TEST_PERSON_MEMORY", make("r_a"))
            assert (await js.get_last_msg("TEST_PERSON_MEMORY", make("r_b"))).data == b"test-only"
        assert (await js.stream_info("TEST_PERSON_MEMORY")).state.messages == 3
    finally:
        if nc:
            await nc.close()
        server.terminate()
        server.wait(timeout=10)
