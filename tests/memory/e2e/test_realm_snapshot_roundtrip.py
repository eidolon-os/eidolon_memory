"""A realm is taken back to a copy of itself, and answers the same afterwards.

§9.5 of the isolation decision sets the standard for the backup work at "can be
restored and was checked", not "can be exported", and this is that check. The
reason it is a real palace rather than a stub is that everything which could
make a restore quietly wrong lives below the file list: a vector store that
opens and finds nothing, a knowledge graph that disagrees with the vectors, an
embedder the collection was not built with. Row counts cannot see any of that.
Recall can.

The shape of the story:

1. A fact is written the only way facts are written — a command through NATS.
2. Recall returns it. This is the answer that has to survive.
3. A copy is taken **while the realm keeps serving**, because a backup that
   needs downtime is a backup nobody takes.
4. The realm is destroyed: the palace and its ledgers are removed with the
   runner stopped, which is what a lost disk looks like from here.
5. The copy is put back, a runner is started on it, and recall answers with the
   same fact.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from eidolon.memory.config.palace_directory import LEDGERS_DIR_SUFFIX
from eidolon.memory.infrastructure.palace_inventory import palace_embedder
from eidolon.memory.infrastructure.realm_snapshot import (
    restore_realm_snapshot,
    write_realm_snapshot,
)
from tests.memory.e2e.conftest import (
    e2e_actor_context,
    mcp_tool_json,
    nats_publish_assertion,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

REALM = "e2e_snapshot_roundtrip"
FACT = "我喜欢喝乌龙茶"


async def _recall_values(session, context, query: str) -> list[str]:
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_recall_context",
            {"query": query, "context": context, "top_k": 5, "voice": False},
        )
    )
    if not isinstance(payload, dict):
        return []
    return [str(record.get("value") or "") for record in payload.get("records") or []]


async def test_a_destroyed_realm_recalls_the_same_after_being_restored(
    live_agent_runner,
    mcp_session,
    tmp_path,
) -> None:
    handle = live_agent_runner(user_id=REALM, steward_mode="test-verbatim")
    context = e2e_actor_context(handle.user_id)

    await nats_publish_assertion(
        handle.nats_url,
        user_id=handle.user_id,
        text=FACT,
        wing="Wing_Profile",
        memory_type="preference",
    )

    async with mcp_session(handle.mcp_url) as session:

        async def _recallable(s) -> bool:
            return FACT in await _recall_values(s, context, "乌龙茶")

        assert await wait_for_visible(session, predicate=_recallable, timeout_s=60), (
            "the fact never became recallable, so there is nothing to prove about restoring it"
        )
        before = await _recall_values(session, context, "乌龙茶")

    palace = handle.palace_dir
    ledgers = palace.with_name(palace.name + LEDGERS_DIR_SUFFIX)
    embedder = palace_embedder(palace)
    assert embedder.source == "palace", (
        "a live palace with no embedder marker would make every real snapshot "
        "refuse; that is a finding about the palace, not about this test"
    )

    # Taken with the runner still serving: no stop, no lock, no reconcile.
    copy = tmp_path / "realm-copy"
    snapshot = write_realm_snapshot(
        palace_path=palace,
        ledgers_path=ledgers,
        destination=copy,
        memory_space_id=handle.user_id,
        embedder_identity=embedder.name,
        embedder_dimension=embedder.dimension,
    )
    assert snapshot.memory_space_id == handle.user_id

    # Now lose the realm. The runner has to be off it first — one process holds
    # a palace — which is the same order a real restore runs in.
    handle.kill()
    await asyncio.sleep(2)
    shutil.rmtree(palace)
    shutil.rmtree(ledgers)

    restored = restore_realm_snapshot(
        source=copy,
        palace_path=palace,
        ledgers_path=ledgers,
        snapshot=snapshot,
    )
    assert restored["file_count"] == len(snapshot.entries)

    revived = live_agent_runner(user_id=REALM, steward_mode="test-verbatim", keep_palace=True)
    async with mcp_session(revived.mcp_url) as session:

        async def _recallable_again(s) -> bool:
            return FACT in await _recall_values(s, context, "乌龙茶")

        assert await wait_for_visible(session, predicate=_recallable_again, timeout_s=60), (
            "the restored realm cannot recall what the copy was taken from — an "
            "export nobody can restore is not a backup"
        )
        assert await _recall_values(session, context, "乌龙茶") == before
