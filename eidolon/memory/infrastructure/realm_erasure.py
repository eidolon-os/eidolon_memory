"""Erase the queued inputs of named Realms so removed memory cannot replay."""

import nats
from eidolon_memory_contracts import (
    conversation_turn_subject,
    memory_command_subject,
    memory_sync_subject,
)
from nats.js.errors import NotFoundError

from eidolon.memory.infrastructure.nats.names import memory_consumer_name


async def purge_realm_messages(
    nats_url: str,
    stream: str,
    durable_prefix: str,
    realm_ids: list[str],
) -> dict[str, list[str]]:
    client = await nats.connect(nats_url)
    deleted_consumers: list[str] = []
    purged_subjects: list[str] = []
    try:
        js = client.jetstream()
        for realm_id in realm_ids:
            for role in ("turn", "cmd", "sync"):
                name = memory_consumer_name(durable_prefix, realm_id, role=role)
                try:
                    await js.delete_consumer(stream, name)
                    deleted_consumers.append(name)
                except NotFoundError:
                    pass
            for subject in (
                conversation_turn_subject(realm_id),
                memory_command_subject(realm_id),
                memory_sync_subject(realm_id),
            ):
                await js.purge_stream(stream, subject=subject)
                purged_subjects.append(subject)
    finally:
        await client.close()
    return {"deleted_consumers": deleted_consumers, "purged_subjects": purged_subjects}
