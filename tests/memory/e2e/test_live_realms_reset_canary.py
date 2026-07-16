"""Operational canary for reset Realms; skipped unless explicitly configured."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from tests.memory.e2e.conftest import (
    mcp_tool_json,
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
