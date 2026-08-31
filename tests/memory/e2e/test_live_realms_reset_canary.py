"""Operational canary for reset Realms; skipped unless explicitly configured."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
import pytest

from eidolon.memory.entrypoints.mcp_server import AGENT_SURFACE_TOOLS
from tests.memory.e2e.conftest import (
    mcp_tool_json,
    nats_publish_assertion,
    nats_publish_commitment,
    nats_publish_exact_correction,
    nats_publish_kg_add_triple,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.live_realm]


def _ops_mcp_url(port: int) -> str:
    """The canary is an operator client, so it only uses the operator surface."""

    return f"http://127.0.0.1:{port}/ops/mcp"


def _realms() -> list[dict[str, object]]:
    raw = os.environ.get("EIDOLON_MEMORY_LIVE_REALMS_JSON", "").strip()
    if not raw:
        pytest.skip("set EIDOLON_MEMORY_LIVE_REALMS_JSON for live Realm canary")
    values = json.loads(raw)
    if not isinstance(values, list) or not values:
        pytest.fail("EIDOLON_MEMORY_LIVE_REALMS_JSON must be a non-empty JSON list")
    return values


async def _snapshot(session) -> tuple[list[dict], list[dict]]:
    listed = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_list",
            {"limit": 5000, "include_private": True},
        )
    )
    kg = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_kg_snapshot",
            {"max_triples": 5000, "current_only": False, "include_sensitive": True},
        )
    )
    return list((listed or {}).get("records") or []), list((kg or {}).get("triples") or [])


async def _wait_for_canary(
    session,
    drawer_marker: str,
    kg_marker: str,
    *,
    attempts: int = 120,
) -> None:
    latest_records: list[dict] = []
    latest_triples: list[dict] = []
    for _ in range(attempts):
        latest_records, latest_triples = await _snapshot(session)
        drawer_ok = any(drawer_marker == str(record.get("value", "")) for record in latest_records)
        kg_ok = any(kg_marker == str(record.get("object", "")) for record in latest_triples)
        if drawer_ok and kg_ok:
            return
        await asyncio.sleep(0.5)
    pytest.fail(
        f"live Realm canary was not visible: drawer={drawer_marker!r} kg={kg_marker!r} "
        f"records={latest_records!r} triples={latest_triples!r}"
    )


async def _wait_for_command(session, request_id: str) -> dict:
    latest: dict = {}
    for _ in range(120):
        payload = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_command_status",
                {"request_id": request_id},
            )
        )
        if isinstance(payload, dict):
            latest = payload
        if latest.get("status") in {"applied", "failed"}:
            return latest
        await asyncio.sleep(0.25)
    pytest.fail(f"live command did not become terminal: {request_id} latest={latest}")


def _chat_turn_id(sse_body: str) -> str:
    for line in sse_body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line.removeprefix("data:").strip())
        turn_id = str(payload.get("turn_id") or "")
        if turn_id:
            return turn_id
    raise AssertionError(f"agent chat response did not contain a turn id: {sse_body}")


async def _wait_for_agent_turn(
    client: httpx.AsyncClient,
    agent_admin_url: str,
    turn_id: str,
) -> dict:
    latest_status = 0
    for _ in range(100):
        response = await client.get(f"{agent_admin_url}/api/admin/conversations/turns/{turn_id}")
        latest_status = response.status_code
        if latest_status == 200:
            return response.json()
        if latest_status != 404:
            response.raise_for_status()
        await asyncio.sleep(0.1)
    raise AssertionError(f"agent turn {turn_id} was not persisted; latest HTTP {latest_status}")


async def test_live_mcp_surface_ownership(mcp_session) -> None:
    """Command status and snapshots are ops contracts, never Agent tools."""

    port = int(_realms()[0]["port"])
    async with mcp_session(f"http://127.0.0.1:{port}/mcp") as agent:
        agent_tools = {tool.name for tool in (await agent.list_tools()).tools}
    async with mcp_session(_ops_mcp_url(port)) as ops:
        ops_tools = {tool.name for tool in (await ops.list_tools()).tools}

    assert agent_tools == set(AGENT_SURFACE_TOOLS)
    assert "eidolon_memory_command_status" not in agent_tools
    assert "eidolon_memory_command_status" in ops_tools
    assert "eidolon_memory_kg_snapshot" in ops_tools


async def test_live_realms_empty_write_visible_and_isolated(mcp_session) -> None:
    realms = _realms()
    verify_existing = os.environ.get("EIDOLON_MEMORY_LIVE_VERIFY_EXISTING", "") == "1"
    empty_only = os.environ.get("EIDOLON_MEMORY_LIVE_EMPTY_ONLY", "") == "1"
    nats_url = os.environ.get("EIDOLON_MEMORY_LIVE_NATS_URL", "nats://127.0.0.1:4222")
    markers: dict[str, tuple[str, str]] = {}

    for item in realms:
        realm_id = str(item["realm_id"])
        port = int(item["port"])
        drawer_marker = f"eidolon-3.5-reset-canary::{realm_id}"
        kg_marker = f"canary:{realm_id}"
        markers[realm_id] = (drawer_marker, kg_marker)
        async with mcp_session(_ops_mcp_url(port)) as session:
            status = mcp_tool_json(await session.call_tool("eidolon_memory_status", {}))
            assert status["memory_space_id"] == realm_id
            assert status["ready"] is True
            records, triples = await _snapshot(session)
            if not verify_existing:
                assert records == [], f"Realm {realm_id} was not empty after reset"
                assert triples == [], f"Realm {realm_id} KG was not empty after reset"
                if empty_only:
                    continue
                await nats_publish_assertion(
                    nats_url,
                    user_id=realm_id,
                    text=drawer_marker,
                    wing="Wing_Profile",
                    memory_type="fact",
                    request_id=f"reset-drawer-{port}",
                )
                await nats_publish_kg_add_triple(
                    nats_url,
                    user_id=realm_id,
                    subject="self",
                    predicate="likes",
                    obj=kg_marker,
                    request_id=f"reset-kg-{port}",
                )
            await _wait_for_canary(
                session,
                drawer_marker,
                kg_marker,
                attempts=10 if verify_existing else 120,
            )

    if empty_only:
        return

    all_drawer_markers = {value[0] for value in markers.values()}
    all_kg_markers = {value[1] for value in markers.values()}
    for item in realms:
        realm_id = str(item["realm_id"])
        port = int(item["port"])
        own_drawer, own_kg = markers[realm_id]
        async with mcp_session(_ops_mcp_url(port)) as session:
            records, triples = await _snapshot(session)
        seen_drawers = {str(record.get("value", "")) for record in records}
        seen_kg = {str(record.get("object", "")) for record in triples}
        assert seen_drawers & all_drawer_markers == {own_drawer}
        assert seen_kg & all_kg_markers == {own_kg}


async def test_live_exact_canonical_dedup_evidence_and_isolation(mcp_session) -> None:
    if os.environ.get("EIDOLON_MEMORY_LIVE_CANONICAL_WRITE") != "1":
        pytest.skip("set EIDOLON_MEMORY_LIVE_CANONICAL_WRITE=1 for write canary")
    realms = _realms()
    if len(realms) < 2:
        pytest.skip("canonical isolation canary requires at least two live Realms")
    nats_url = os.environ.get(
        "EIDOLON_MEMORY_LIVE_NATS_URL",
        "nats://127.0.0.1:4222",
    )
    target = realms[0]
    target_realm = str(target["realm_id"])
    target_port = int(target["port"])
    marker = f"live-canonical-{uuid.uuid4().hex[:12]}"

    async with mcp_session(_ops_mcp_url(target_port)) as session:
        before = mcp_tool_json(await session.call_tool("eidolon_memory_canonical_stats", {}))
        first_id = await nats_publish_assertion(
            nats_url,
            user_id=target_realm,
            text=f"self likes {marker}",
            request_id=f"live-canonical-1-{uuid.uuid4().hex}",
            subject="self",
            predicate="likes",
            object_value=marker,
        )
        second_id = await nats_publish_assertion(
            nats_url,
            user_id=target_realm,
            text=f"self likes {marker}",
            request_id=f"live-canonical-2-{uuid.uuid4().hex}",
            subject="self",
            predicate="likes",
            object_value=marker,
        )
        first = await _wait_for_command(session, first_id)
        second = await _wait_for_command(session, second_id)
        assert first["status"] == "applied", first
        assert second["status"] == "applied", second
        assert str(second.get("resource_id") or "").endswith(":evidence:2")

        after = mcp_tool_json(await session.call_tool("eidolon_memory_canonical_stats", {}))
        assert after["assertions_total"] == before["assertions_total"] + 1
        assert after["evidence_total"] == before["evidence_total"] + 2
        assert after["kg_not_projected"] == 0
        records, triples = await _snapshot(session)
        assert sum(marker in str(row.get("value") or "") for row in records) == 1
        assert (
            sum(
                row.get("subject") == "self"
                and row.get("predicate") == "likes"
                and row.get("object") == marker
                and row.get("valid_to") is None
                for row in triples
            )
            == 1
        )

    for item in realms[1:]:
        async with mcp_session(_ops_mcp_url(int(item["port"]))) as session:
            records, triples = await _snapshot(session)
        assert not any(marker in str(row.get("value") or "") for row in records)
        assert not any(row.get("object") == marker for row in triples)


async def test_live_exact_canonical_invalidation_archives_current_projection(
    mcp_session,
) -> None:
    if os.environ.get("EIDOLON_MEMORY_LIVE_CANONICAL_WRITE") != "1":
        pytest.skip("set EIDOLON_MEMORY_LIVE_CANONICAL_WRITE=1 for write canary")
    target = _realms()[0]
    realm_id = str(target["realm_id"])
    port = int(target["port"])
    nats_url = os.environ.get(
        "EIDOLON_MEMORY_LIVE_NATS_URL",
        "nats://127.0.0.1:4222",
    )
    marker = f"live-invalidation-{uuid.uuid4().hex[:12]}"

    async with mcp_session(_ops_mcp_url(port)) as session:
        before = mcp_tool_json(await session.call_tool("eidolon_memory_canonical_stats", {}))
        add_id = await nats_publish_assertion(
            nats_url,
            user_id=realm_id,
            text=f"self likes {marker}",
            request_id=f"live-invalidation-add-{uuid.uuid4().hex}",
            subject="self",
            predicate="likes",
            object_value=marker,
        )
        assert (await _wait_for_command(session, add_id))["status"] == "applied"

        correction_id = await nats_publish_exact_correction(
            nats_url,
            user_id=realm_id,
            subject="self",
            predicate="likes",
            object_value=marker,
            text=f"self no longer likes {marker}",
            request_id=f"live-invalidation-end-{uuid.uuid4().hex}",
        )
        correction = await _wait_for_command(session, correction_id)
        assert correction["status"] == "applied", correction

        after = mcp_tool_json(await session.call_tool("eidolon_memory_canonical_stats", {}))
        assert after["assertions_total"] == before["assertions_total"] + 1
        assert after["assertions_invalidated"] == before["assertions_invalidated"] + 1
        assert after["invalidations_total"] == before["invalidations_total"] + 1
        records, triples = await _snapshot(session)
        drawer = next(row for row in records if marker in str(row.get("value") or ""))
        assert (drawer.get("metadata") or {}).get("privacy") == "do_not_recall"
        historical = [
            row
            for row in triples
            if row.get("subject") == "self"
            and row.get("predicate") == "likes"
            and row.get("object") == marker
        ]
        assert len(historical) == 1
        assert historical[0].get("valid_to") is not None

        current = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_kg_snapshot",
                {
                    "max_triples": 5000,
                    "current_only": True,
                    "include_sensitive": True,
                },
            )
        )
        assert not any(row.get("object") == marker for row in (current or {}).get("triples") or [])


async def test_live_explicit_single_slot_update_retains_superseded_history(
    mcp_session,
) -> None:
    if os.environ.get("EIDOLON_MEMORY_LIVE_CANONICAL_WRITE") != "1":
        pytest.skip("set EIDOLON_MEMORY_LIVE_CANONICAL_WRITE=1 for write canary")
    target = _realms()[0]
    realm_id = str(target["realm_id"])
    port = int(target["port"])
    nats_url = os.environ.get(
        "EIDOLON_MEMORY_LIVE_NATS_URL",
        "nats://127.0.0.1:4222",
    )
    marker = uuid.uuid4().hex[:12]
    subject = f"person:update-canary:{marker}"
    old_city = f"old-city:{marker}"
    new_city = f"new-city:{marker}"

    async with mcp_session(_ops_mcp_url(port)) as session:
        before = mcp_tool_json(await session.call_tool("eidolon_memory_canonical_stats", {}))
        add_id = await nats_publish_assertion(
            nats_url,
            user_id=realm_id,
            text=f"{subject} lives in {old_city}",
            request_id=f"live-update-add-{uuid.uuid4().hex}",
            subject=subject,
            predicate="lives_in",
            object_value=old_city,
        )
        assert (await _wait_for_command(session, add_id))["status"] == "applied"

        update_id = await nats_publish_assertion(
            nats_url,
            user_id=realm_id,
            text=f"{subject} now lives in {new_city}",
            request_id=f"live-update-replace-{uuid.uuid4().hex}",
            subject=subject,
            predicate="lives_in",
            object_value=new_city,
            operation_hint="update",
        )
        update = await _wait_for_command(session, update_id)
        assert update["status"] == "applied", update

        after = mcp_tool_json(await session.call_tool("eidolon_memory_canonical_stats", {}))
        assert after["assertions_total"] == before["assertions_total"] + 2
        assert after["assertions_active"] == before["assertions_active"] + 1
        assert after["assertions_superseded"] == before["assertions_superseded"] + 1
        assert after["supersessions_total"] == before["supersessions_total"] + 1
        assert after["invalidations_total"] == before["invalidations_total"]

        records, historical = await _snapshot(session)
        old_drawer = next(row for row in records if old_city in str(row.get("value") or ""))
        assert (old_drawer.get("metadata") or {}).get("privacy") == "do_not_recall"
        old_rows = [
            row
            for row in historical
            if row.get("subject") == subject
            and row.get("predicate") == "lives_in"
            and row.get("object") == old_city
        ]
        assert len(old_rows) == 1
        assert old_rows[0].get("valid_to") is not None

        current = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_kg_snapshot",
                {
                    "max_triples": 5000,
                    "current_only": True,
                    "include_sensitive": True,
                },
            )
        )
        current_rows = list((current or {}).get("triples") or [])
        assert not any(row.get("object") == old_city for row in current_rows)
        assert any(
            row.get("subject") == subject
            and row.get("predicate") == "lives_in"
            and row.get("object") == new_city
            for row in current_rows
        )


async def test_live_exact_fact_reactivation_creates_new_validity_period(
    mcp_session,
) -> None:
    if os.environ.get("EIDOLON_MEMORY_LIVE_CANONICAL_WRITE") != "1":
        pytest.skip("set EIDOLON_MEMORY_LIVE_CANONICAL_WRITE=1 for write canary")
    target = _realms()[0]
    realm_id = str(target["realm_id"])
    port = int(target["port"])
    nats_url = os.environ.get(
        "EIDOLON_MEMORY_LIVE_NATS_URL",
        "nats://127.0.0.1:4222",
    )
    marker = uuid.uuid4().hex[:12]
    subject = f"person:reactivation-canary:{marker}"
    object_value = f"topic:reactivation:{marker}"

    async with mcp_session(_ops_mcp_url(port)) as session:
        before = mcp_tool_json(await session.call_tool("eidolon_memory_canonical_stats", {}))
        add_id = await nats_publish_assertion(
            nats_url,
            user_id=realm_id,
            text=f"{subject} likes {object_value}",
            request_id=f"live-reactivation-add-{uuid.uuid4().hex}",
            subject=subject,
            predicate="likes",
            object_value=object_value,
        )
        assert (await _wait_for_command(session, add_id))["status"] == "applied"

        correction_id = await nats_publish_exact_correction(
            nats_url,
            user_id=realm_id,
            subject=subject,
            predicate="likes",
            object_value=object_value,
            text=f"{subject} no longer likes {object_value}",
            request_id=f"live-reactivation-end-{uuid.uuid4().hex}",
        )
        assert (await _wait_for_command(session, correction_id))["status"] == "applied"

        reactivation_id = await nats_publish_assertion(
            nats_url,
            user_id=realm_id,
            text=f"{subject} likes {object_value} again",
            request_id=f"live-reactivation-again-{uuid.uuid4().hex}",
            subject=subject,
            predicate="likes",
            object_value=object_value,
            operation_hint="update",
        )
        reactivated = await _wait_for_command(session, reactivation_id)
        assert reactivated["status"] == "applied", reactivated
        assert str(reactivated.get("resource_id") or "").startswith("reactivated:")

        after = mcp_tool_json(await session.call_tool("eidolon_memory_canonical_stats", {}))
        assert after["assertions_total"] == before["assertions_total"] + 1
        assert after["assertions_active"] == before["assertions_active"] + 1
        assert after["reactivations_total"] == before["reactivations_total"] + 1
        assert after["reactivations_pending"] == 0

        history = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_fact_history",
                {
                    "subject": subject,
                    "predicate": "likes",
                    "object_value": object_value,
                },
            )
        )
        assert history["status"] == "ok"
        fact = history["facts"][0]
        assert fact["fact"]["state"] == "active"
        assert fact["fact"]["projection_id"].endswith(":activation:2")
        assert [row["transition"] for row in fact["transitions"]] == [
            "invalidated",
            "reactivated",
        ]
        assert len(fact["evidence"]) == 2

        records, triples = await _snapshot(session)
        drawers = [row for row in records if object_value in str(row.get("value") or "")]
        assert len(drawers) == 2
        assert (
            sum((row.get("metadata") or {}).get("privacy") == "do_not_recall" for row in drawers)
            == 1
        )
        exact_rows = [
            row
            for row in triples
            if row.get("subject") == subject
            and row.get("predicate") == "likes"
            and row.get("object") == object_value
        ]
        assert len(exact_rows) == 2
        assert sum(row.get("valid_to") is None for row in exact_rows) == 1
        assert sum(row.get("valid_to") is not None for row in exact_rows) == 1


async def test_live_commitment_supplement_and_fulfilment_lifecycle(
    mcp_session,
) -> None:
    if os.environ.get("EIDOLON_MEMORY_LIVE_CANONICAL_WRITE") != "1":
        pytest.skip("set EIDOLON_MEMORY_LIVE_CANONICAL_WRITE=1 for write canary")
    target = _realms()[0]
    realm_id = str(target["realm_id"])
    port = int(target["port"])
    nats_url = os.environ.get(
        "EIDOLON_MEMORY_LIVE_NATS_URL",
        "nats://127.0.0.1:4222",
    )
    marker = uuid.uuid4().hex[:12]
    subject = f"person:commitment-canary:{marker}"
    action = f"带 companion:test 去恐龙园 {marker}"
    beneficiary = f"companion:test:{marker}"

    async with mcp_session(_ops_mcp_url(port)) as session:
        create_id = await nats_publish_commitment(
            nats_url,
            user_id=realm_id,
            subject=subject,
            action=action,
            text=f"以后我带你去恐龙园 {marker}",
            operation_hint="confirm",
            request_id=f"live-commitment-create-{uuid.uuid4().hex}",
            beneficiaries=[beneficiary],
        )
        created = await _wait_for_command(session, create_id)
        assert created["status"] == "applied", created
        resource_id = str(created.get("resource_id") or "")
        commitment_id = resource_id.split(":revision:", 1)[0]
        assert commitment_id.startswith("commitment:")

        supplement_id = await nats_publish_commitment(
            nats_url,
            user_id=realm_id,
            subject=subject,
            action=action,
            text=f"到时候带朋友一起 {marker}",
            operation_hint="update",
            request_id=f"live-commitment-supplement-{uuid.uuid4().hex}",
            target_id=commitment_id,
            participants=[f"friend:小明:{marker}", f"friend:小红:{marker}"],
        )
        supplemented = await _wait_for_command(session, supplement_id)
        assert supplemented["status"] == "applied", supplemented

        current = mcp_tool_json(await session.call_tool("eidolon_memory_commitments", {}))
        commitment = next(
            row for row in current["commitments"] if row["commitment_id"] == commitment_id
        )
        assert commitment["status"] == "confirmed"
        assert commitment["revision"] == 2
        assert commitment["participants"] == [
            f"friend:小明:{marker}",
            f"friend:小红:{marker}",
        ]
        assert commitment["drawer_projection_state"] == "projected"
        assert commitment["kg_projection_state"] == "projected"

        fulfil_id = await nats_publish_commitment(
            nats_url,
            user_id=realm_id,
            subject=subject,
            action=action,
            text=f"我们已经去过恐龙园了 {marker}",
            operation_hint="update",
            request_id=f"live-commitment-fulfil-{uuid.uuid4().hex}",
            target_id=commitment_id,
            status="fulfilled",
        )
        fulfilled = await _wait_for_command(session, fulfil_id)
        assert fulfilled["status"] == "applied", fulfilled

        current = mcp_tool_json(await session.call_tool("eidolon_memory_commitments", {}))
        assert not any(row["commitment_id"] == commitment_id for row in current["commitments"])
        history = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_commitment_history",
                {"commitment_id": commitment_id},
            )
        )
        assert history["status"] == "ok"
        assert [row["status"] for row in history["revisions"]] == [
            "confirmed",
            "confirmed",
            "fulfilled",
        ]

        records, triples = await _snapshot(session)
        drawers = [row for row in records if marker in str(row.get("value") or "")]
        assert len(drawers) == 2
        assert all((row.get("metadata") or {}).get("privacy") == "do_not_recall" for row in drawers)
        assert not any(
            row.get("subject") == subject
            and row.get("predicate") == "promised"
            and row.get("object") == action
            and row.get("valid_to") is None
            for row in triples
        )


async def test_live_commitment_reaches_agent_product_context(mcp_session) -> None:
    """Dogfood the deployed NATS → Realm → Agent chat context path."""
    if os.environ.get("EIDOLON_MEMORY_LIVE_CANONICAL_WRITE") != "1":
        pytest.skip("set EIDOLON_MEMORY_LIVE_CANONICAL_WRITE=1 for write canary")
    owner_id = os.environ.get("EIDOLON_MEMORY_LIVE_OWNER_ID", "").strip()
    companion_id = os.environ.get("EIDOLON_MEMORY_LIVE_COMPANION_ID", "").strip()
    if not owner_id or not companion_id:
        pytest.skip("set live owner and companion ids for Agent context dogfood")

    target = _realms()[0]
    realm_id = str(target["realm_id"])
    port = int(target["port"])
    nats_url = os.environ.get(
        "EIDOLON_MEMORY_LIVE_NATS_URL",
        "nats://127.0.0.1:4222",
    )
    agent_admin_url = os.environ.get(
        "EIDOLON_MEMORY_LIVE_AGENT_ADMIN_URL",
        "http://127.0.0.1:8081",
    ).rstrip("/")
    marker = uuid.uuid4().hex[:12]
    action = f"dogfood commitment context {marker}"
    commitment_id = ""

    async with mcp_session(_ops_mcp_url(port)) as session:
        try:
            create_id = await nats_publish_commitment(
                nats_url,
                user_id=realm_id,
                subject=companion_id,
                action=action,
                text=f"显式创建陪伴上下文 dogfood {marker}",
                operation_hint="confirm",
                request_id=f"live-context-create-{uuid.uuid4().hex}",
                beneficiaries=[owner_id],
                due_at="2026-07-17T00:00:00+08:00",
            )
            created = await _wait_for_command(session, create_id)
            assert created["status"] == "applied", created
            commitment_id = str(created.get("resource_id") or "").split(":revision:", 1)[0]
            assert commitment_id.startswith("commitment:")

            async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
                chat = await client.post(
                    f"{agent_admin_url}/api/admin/chat/test",
                    json={
                        "owner_id": owner_id,
                        "companion_id": companion_id,
                        "text": "只回复收到，不执行任何背景计划。",
                        "persist_memory": False,
                    },
                )
                chat.raise_for_status()
                turn_id = _chat_turn_id(chat.text)
                detail = await _wait_for_agent_turn(
                    client,
                    agent_admin_url,
                    turn_id,
                )

            assert detail["memory_realm_id"] == realm_id
            trace = detail["metadata"]["commitment_context_trace"]
            assert trace["attempted"] is True
            assert trace["degraded"] is False
            assert trace["context_injected"] is True
            assert commitment_id in trace["commitment_ids"]
            commitment_segments = [
                row
                for row in detail["metadata"]["context_ledger"]["segments"]
                if row["kind"] == "commitment"
            ]
            assert len(commitment_segments) == 1
            assert commitment_segments[0]["metadata"]["actionability"] == "must_not_execute"
        finally:
            if commitment_id:
                fulfil_id = await nats_publish_commitment(
                    nats_url,
                    user_id=realm_id,
                    subject=companion_id,
                    action=action,
                    text=f"清理陪伴上下文 dogfood {marker}",
                    operation_hint="update",
                    request_id=f"live-context-fulfil-{uuid.uuid4().hex}",
                    target_id=commitment_id,
                    status="fulfilled",
                )
                fulfilled = await _wait_for_command(session, fulfil_id)
                assert fulfilled["status"] == "applied", fulfilled
