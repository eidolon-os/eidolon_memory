"""MCP recall probes must not turn dependency failures into successful latency."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from eidolon_memory_contracts import build_memory_actor_context

from scripts.benchmark import bench_read_livekit as bench


def response(**updates):
    data = {"records": [], "kg_triples": [], "degraded": False, "trace": {}}
    data.update(updates)
    return SimpleNamespace(isError=False, structuredContent=data, content=[])


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(isError=True, structuredContent=None, content=[]),
        SimpleNamespace(isError=False, structuredContent=None, content=[]),
        SimpleNamespace(isError=False, structuredContent={"records": []}, content=[]),
    ],
)
def test_decode_rejects_tool_errors_and_incomplete_responses(result):
    with pytest.raises(ValueError):
        bench._decode_recall(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "tool", "malformed", "degraded", "warmup"])
async def test_failed_sample_never_passes_latency_gate(monkeypatch, failure):
    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield None, None, None

    calls = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, name, arguments):
            calls.append(arguments)
            fail_at = 1 if failure == "warmup" else 2
            if len(calls) != fail_at:
                return response(records=[{"key": "fact"}])
            if failure == "transport":
                raise TimeoutError("provider timed out")
            if failure == "tool":
                return SimpleNamespace(isError=True, content=[])
            if failure == "malformed":
                return SimpleNamespace(isError=False, structuredContent={}, content=[])
            return response(degraded=True, degraded_reason="backend unavailable")

    monkeypatch.setattr(bench, "streamablehttp_client", transport)
    monkeypatch.setattr(bench, "ClientSession", lambda *args: Session())
    context = build_memory_actor_context(
        memory_realm_id="bench",
        owner_id="owner",
        companion_id="companion",
        device_id="device",
        session_id="session",
    )
    result = await bench._run(
        "http://localhost/mcp",
        count=2,
        warmup=1,
        queries=["tea"],
        voice=True,
        with_kg=True,
        context=context,
    )
    assert result["sla"] == "FAIL"
    assert len(result["per_call"]) == 3
    assert result["summary"]["latency_ms"]["count"] == 2
    assert calls[0]["context"]["memory_realm_id"] == "bench"
    assert calls[0]["context"]["companion_id"] == "companion"
    if failure == "warmup":
        assert result["warmup"]["degraded"] == 1
        assert result["summary"]["degraded"] == 0
    elif failure == "degraded":
        assert result["summary"]["degraded"] == 1
    else:
        assert result["summary"]["errors"] == 1


def test_empty_is_valid_and_distinct_from_error():
    assert bench._decode_recall(response())["records"] == []
    summary = bench._summarize(
        [
            {
                "error": None,
                "degraded": False,
                "records": [],
                "kg_triples": [],
                "elapsed_ms": 4,
            }
        ]
    )
    assert summary["empty"] == 1
    assert summary["errors"] == summary["degraded"] == 0
