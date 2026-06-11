"""Adapter parsing for MCP search payloads."""

from __future__ import annotations

from eidolon.memory.adapters import parse_search_tool_payload


def test_parse_list_of_dicts():
    data = [
        {"wing": "u1", "room": "r1", "text": "hello", "distance": 0.1},
    ]
    recs = parse_search_tool_payload(data)
    assert len(recs) == 1
    assert recs[0].user_id == "u1"
    assert recs[0].key == "r1"


def test_parse_wrapped_results():
    data = {"results": [{"wing": "a", "room": "b", "text": "{}"}]}
    recs = parse_search_tool_payload(data)
    assert recs[0].value == {}


def test_parse_search_payload_derives_public_memory_time():
    data = {
        "results": [
            {
                "wing": "Wing_Life",
                "room": "pet_iron",
                "text": "铁锤今天去洗澡",
                "created_at": "2026-05-19T10:01:00Z",
                "metadata": {"occurred_at": "2026-05-18T20:00:00Z"},
            }
        ]
    }
    recs = parse_search_tool_payload(data)
    assert recs[0].memory_time is not None
    assert recs[0].memory_time.isoformat() == "2026-05-18T20:00:00+00:00"
    assert recs[0].memory_time_source == "occurred_at"
    assert recs[0].created_at is not None
