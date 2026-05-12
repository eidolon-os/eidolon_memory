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
