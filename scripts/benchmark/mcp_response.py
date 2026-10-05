"""Validate the recall response shared by latency and quality benchmarks."""

import json


def decode_recall_response(result) -> dict:
    if result.isError:
        raise ValueError(f"MCP tool error: {result.content}")
    data = result.structuredContent
    if data is None:
        texts = [block.text for block in result.content if block.type == "text"]
        if len(texts) != 1:
            raise ValueError("expected one JSON recall response")
        data = json.loads(texts[0])
    if not isinstance(data, dict):
        raise ValueError("recall response must be an object")
    for field in ("records", "kg_triples"):
        if not isinstance(data.get(field), list):
            raise ValueError(f"recall response missing {field} list")
    if not isinstance(data.get("degraded"), bool) or not isinstance(data.get("trace"), dict):
        raise ValueError("recall response missing degraded/trace")
    return data

