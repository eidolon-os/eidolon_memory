"""Operational canary for reset Realms; skipped unless explicitly configured."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from tests.memory.e2e.conftest import (
    mcp_tool_json,
    nats_publish_exact_correction,
    nats_publish_kg_add_triple,
    nats_publish_user_confirm,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.live_realm]


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
        drawer_ok = any(
            drawer_marker == str(record.get("value", "")) for record in latest_records
        )
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
        async with mcp_session(f"http://127.0.0.1:{port}/mcp") as session:
            status = mcp_tool_json(await session.call_tool("eidolon_memory_status", {}))
            assert status["memory_space_id"] == realm_id
            assert status["ready"] is True
            records, triples = await _snapshot(session)
            if not verify_existing:
                assert records == [], f"Realm {realm_id} was not empty after reset"
                assert triples == [], f"Realm {realm_id} KG was not empty after reset"
                if empty_only:
                    continue
                await nats_publish_user_confirm(
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
        async with mcp_session(f"http://127.0.0.1:{port}/mcp") as session:
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

    async with mcp_session(f"http://127.0.0.1:{target_port}/mcp") as session:
        before = mcp_tool_json(
            await session.call_tool("eidolon_memory_canonical_stats", {})
        )
        first_id = await nats_publish_user_confirm(
            nats_url,
            user_id=target_realm,
            text=f"self likes {marker}",
            request_id=f"live-canonical-1-{uuid.uuid4().hex}",
            subject="self",
            predicate="likes",
            object_value=marker,
        )
        second_id = await nats_publish_user_confirm(
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

        after = mcp_tool_json(
            await session.call_tool("eidolon_memory_canonical_stats", {})
        )
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
        async with mcp_session(
            f"http://127.0.0.1:{int(item['port'])}/mcp"
        ) as session:
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

    async with mcp_session(f"http://127.0.0.1:{port}/mcp") as session:
        before = mcp_tool_json(
            await session.call_tool("eidolon_memory_canonical_stats", {})
        )
        add_id = await nats_publish_user_confirm(
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

        after = mcp_tool_json(
            await session.call_tool("eidolon_memory_canonical_stats", {})
        )
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
        assert not any(
            row.get("object") == marker
            for row in (current or {}).get("triples") or []
        )
