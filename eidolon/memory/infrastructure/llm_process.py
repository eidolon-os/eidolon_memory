"""Bounded model execution outside the event loop serving Memory reads.

The child owns SDK initialization, provider retries and HTTP connections, never
palace or ledger handles. One request at a time provides backpressure; NATS is
still the durable queue and the caller still owns extraction decisions. A failed
or cancelled exchange retires the child so a late answer cannot serve a new turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sys
from typing import Any


class ModelExecutionError(RuntimeError):
    """A retryable model failure, not an empty extraction decision."""


class IsolatedLLMCompletion:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._request: asyncio.Task | None = None
        self._closed = False

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        async with self._lock:
            if self._closed:
                raise ModelExecutionError("model executor is closed")
            timeout = float(kwargs["timeout"])
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("model execution timeout must be finite and positive")
            self._request = asyncio.create_task(self._exchange(kwargs))
            try:
                # Includes cold initialization and SDK retries. Queue wait is
                # backpressure, and does not spend this turn's execution budget.
                return await asyncio.wait_for(self._request, timeout=timeout)
            except BaseException as exc:
                await self._stop_process()
                if isinstance(exc, TimeoutError):
                    raise TimeoutError(f"model execution exceeded {timeout:g} seconds") from exc
                raise
            finally:
                self._request = None

    async def _exchange(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self._process is None:
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "eidolon.memory.infrastructure.llm_worker",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    # SDK diagnostics go to the runner's log, not the RPC pipe.
                    stderr=None,
                    limit=4 * 1024 * 1024,
                )
            )
            try:
                self._process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Cancellation must not lose a child being created by asyncio.
                self._process = await spawn
                raise
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(kwargs, ensure_ascii=False).encode() + b"\n")
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line:
            raise ModelExecutionError("model executor exited without a response")
        payload = json.loads(line)
        if "error" in payload:
            raise ModelExecutionError(f"model execution failed: {payload['error']}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ModelExecutionError("invalid model executor response")
        return result

    async def _stop_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        # Drain the pipe as well as reaping: wait() alone can hang if an invalid
        # oversized response filled asyncio's read buffer before cancellation.
        await process.communicate()

    async def aclose(self) -> None:
        self._closed = True
        if self._request is not None and not self._request.cancelling():
            self._request.cancel()
        async with self._lock:
            await self._stop_process()
