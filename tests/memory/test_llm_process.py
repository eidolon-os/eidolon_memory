"""Blocking provider work must not monopolize the caller's event loop."""

import asyncio
import os
import time

import pytest

from eidolon.memory.infrastructure.llm_process import IsolatedLLMCompletion, ModelExecutionError
from tests.memory.llm_fixture import model_sdk_fixture  # noqa: F401


async def _event(path, name):
    async with asyncio.timeout(5):
        while not path.exists() or name not in path.read_text():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("api_base", ["http://127.0.0.1:8769/v1", "https://cloud.invalid/v1"])
async def test_cold_and_slow_model_do_not_block_reads(isolated_model_sdk, monkeypatch, api_base):
    monkeypatch.setenv("TEST_LLM_IMPORT_DELAY", "0.7")
    monkeypatch.setenv("TEST_LLM_CALL_DELAY", "0.7")
    client = IsolatedLLMCompletion()
    task = asyncio.create_task(client(model="test", api_base=api_base, timeout=5))
    try:
        for stage in ("import_started", "call_started"):
            await _event(isolated_model_sdk, stage)
            assert not task.done(), "blocking model work ran on the read loop"
            start = time.monotonic()
            await asyncio.sleep(0.05)
            assert time.monotonic() - start < 0.3
        response = await task
        assert response["provider_pid"] != os.getpid()
        assert response["api_base"] == api_base
        # Reuse the initialized process and its provider connections.
        second = await client(model="test", api_base=api_base, timeout=5)
        assert second["provider_pid"] == response["provider_pid"]
    finally:
        await client.aclose()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("action", ["timeout", "cancel", "close", "exit", "error", "oversized"])
async def test_failed_work_is_reaped_and_cannot_answer_the_next_turn(
    isolated_model_sdk, monkeypatch, action
):
    spawned = []
    original_spawn = asyncio.create_subprocess_exec

    async def track_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track_spawn)
    if action in ("timeout", "cancel", "close"):
        monkeypatch.setenv("TEST_LLM_CALL_DELAY", "10")
    client = IsolatedLLMCompletion()
    task = asyncio.create_task(client(model=action, timeout=0.5 if action == "timeout" else 30))
    try:
        await _event(isolated_model_sdk, "call_started")
        if action == "cancel":
            task.cancel()
        elif action == "close":
            await asyncio.wait_for(client.aclose(), 1)
        expected = {
            "timeout": TimeoutError,
            "cancel": asyncio.CancelledError,
            "close": asyncio.CancelledError,
            "exit": ModelExecutionError,
            "error": ModelExecutionError,
            "oversized": ValueError,
        }[action]
        with pytest.raises(expected) as caught:
            await asyncio.wait_for(task, timeout=5)
        process = spawned[0]
        assert "credential-must-not-be-returned" not in str(caught.value)
        assert process.returncode is not None
        if action == "close":
            with pytest.raises(ModelExecutionError, match="closed"):
                await client(model="test", timeout=5)
        else:
            monkeypatch.setenv("TEST_LLM_CALL_DELAY", "0")
            result = await client(model="recovered", timeout=5)
            assert result["model"] == "recovered"
            assert result["provider_pid"] != process.pid
    finally:
        await client.aclose()


async def test_cancellation_during_spawn_does_not_lose_the_child(isolated_model_sdk, monkeypatch):
    spawned = []
    ready, release = asyncio.Event(), asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        spawned.append(process)
        ready.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    client = IsolatedLLMCompletion()
    task = asyncio.create_task(client(model="test", timeout=5))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawned[0].returncode is not None
    finally:
        release.set()
        await client.aclose()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_turns_are_serialized_without_blocking_reads(
    isolated_model_sdk, monkeypatch
):
    monkeypatch.setenv("TEST_LLM_CALL_DELAY", "0.2")
    client = IsolatedLLMCompletion()
    try:
        results = await asyncio.gather(*(client(model=str(i), timeout=5) for i in range(3)))
        assert [r["model"] for r in results] == ["0", "1", "2"]
        assert len({r["provider_pid"] for r in results}) == 1
        assert isolated_model_sdk.read_text().splitlines() == [
            "import_started",
            "import_finished",
            "call_started",
            "call_finished",
            "call_started",
            "call_finished",
            "call_started",
            "call_finished",
        ]
    finally:
        await client.aclose()
