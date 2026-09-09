"""Actual LiteLLM SDK, with an isolated local HTTP model endpoint."""

import asyncio
import json

from eidolon.memory.infrastructure.llm_process import IsolatedLLMCompletion


async def test_real_sdk_response_and_slow_http_are_isolated(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    # A workstation proxy must not intercept this isolated loopback fixture.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    received = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def serve(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            size = next(
                int(line.split(b":", 1)[1])
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            request = json.loads(await reader.readexactly(size))
            received.set_result(request)
            await release.wait()
            body = json.dumps(
                {
                    "id": "chatcmpl-isolation",
                    "object": "chat.completion",
                    "created": 0,
                    "model": request["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "isolated response"},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = IsolatedLLMCompletion()
    task = asyncio.create_task(
        client(
            model="openai/isolation-probe",
            api_base=f"http://127.0.0.1:{port}/v1",
            api_key="test-only",
            messages=[{"role": "user", "content": "test"}],
            timeout=30,
        )
    )
    try:
        done, _ = await asyncio.wait(
            (received, task), timeout=25, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            await task  # Surface provider errors rather than hiding them behind a test timeout.
        request = await asyncio.wait_for(received, 25)
        assert request["model"] == "isolation-probe"
        assert not task.done()
        # Real network wait in the model process cannot block the read loop.
        await asyncio.wait_for(asyncio.sleep(0.05), timeout=0.3)
        release.set()
        result = await task
        assert result["choices"][0]["message"]["content"] == "isolated response"
    finally:
        release.set()
        await client.aclose()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
