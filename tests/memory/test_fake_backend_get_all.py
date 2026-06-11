"""Fake backend get_all semantics (tenant = metadata.user_id OR wing legacy)."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend


@pytest.mark.asyncio
async def test_get_all_legacy_wing_only_tenant_match():
    b = FakeMemoryBackend()
    await b.ingest_text(wing="default", room="general", text="nats_style", metadata=None)
    rows = await b.get_all("default")
    assert len(rows) == 1
    assert "nats" in str(rows[0].value).lower()


@pytest.mark.asyncio
async def test_get_all_steward_like_metadata_user_id():
    b = FakeMemoryBackend()
    await b.ingest_text(
        wing="Wing_Profile",
        room="profile_core",
        text="prefers dark mode",
        metadata={"user_id": "carol"},
    )
    by_user = await b.get_all("carol")
    assert len(by_user) == 1
    by_wing = await b.get_all("Wing_Profile")
    assert len(by_wing) == 1


@pytest.mark.asyncio
async def test_get_all_blank_lists_entire_fake_store():
    b = FakeMemoryBackend()
    await b.ingest_text(wing="Wing_Profile", room="a", text="one", metadata=None)
    await b.ingest_text(wing="Wing_Event", room="b", text="two", metadata=None)
    rows = await b.get_all("")
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_get_all_pagination_slice():
    b = FakeMemoryBackend()
    for i in range(4):
        await b.ingest_text(wing="t_tenant", room=f"slot_{i}", text=str(i), metadata=None)
    page0 = await b.get_all("t_tenant", limit=2, offset=0)
    page1 = await b.get_all("t_tenant", limit=2, offset=2)
    assert len(page0) == 2
    assert len(page1) == 2
    texts = sorted({str(r.value) for r in page0 + page1})
    assert texts == ["0", "1", "2", "3"]


@pytest.mark.asyncio
async def test_ingest_text_preserves_canonical_memory_time_and_indexed_at():
    b = FakeMemoryBackend()
    await b.ingest_text(
        wing="Wing_Life",
        room="pet_iron",
        text="铁锤今天去洗澡",
        metadata={"occurred_at": "2026-05-18T20:00:00Z"},
    )
    rows = await b.get_all("")
    assert len(rows) == 1
    assert rows[0].memory_time is not None
    assert rows[0].memory_time.isoformat() == "2026-05-18T20:00:00+00:00"
    assert rows[0].memory_time_source == "occurred_at"
    assert rows[0].metadata["filed_at"] == "2026-05-18T20:00:00Z"
    assert rows[0].metadata["indexed_at"]
