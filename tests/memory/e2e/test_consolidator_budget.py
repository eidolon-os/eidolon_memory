"""Deterministic E2E for bounded, partially successful consolidation."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.memory.e2e.conftest import (
    mcp_tool_json,
    nats_publish_assertion,
    tail_file,
    wait_for_visible,
)
from tests.memory.e2e.test_consolidator_lifecycle import _run_consolidator

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


class _SlowCompletionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class) -> None:
        super().__init__(server_address, handler_class)
        self.request_count = 0
        self.request_lock = threading.Lock()
        self.request_paths: list[str] = []


class _CompletionHandler(BaseHTTPRequestHandler):
    server: _SlowCompletionServer

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        with self.server.request_lock:
            self.server.request_count += 1
            self.server.request_paths.append(self.path)
            request_number = self.server.request_count
        if request_number > 1:
            time.sleep(20.0)
        body = json.dumps(
            {
                "id": f"local-{request_number}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "test-memory",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "themes": [
                                        {
                                            "text": "你最近持续关注工作节奏与个人状态。",
                                            "confidence": 0.9,
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
            ensure_ascii=False,
        ).encode()
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format: str, *args) -> None:
        del args


@contextmanager
def _slow_completion_server():
    server = _SlowCompletionServer(("127.0.0.1", 0), _CompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_consolidator_exits_on_budget_and_publishes_completed_wing(
    live_agent_runner,
    mcp_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("EIDOLON_MEMORY_LLM_API_KEY", "local-test-key")
    with _slow_completion_server() as (api_base, completion_server):
        handle = live_agent_runner(
            user_id="e2e_consolidator_budget",
            steward_mode="noop",
            extra_settings={
                "llm": {
                    "model": "openai/test-memory",
                    "base_url": api_base,
                    # Keep the per-call timeout above the pass budget so this
                    # scenario specifically exercises pass-level cancellation.
                    "timeout_seconds": 30,
                }
            },
        )
        facts = [
            ("Wing_Work", "工作节奏最近需要重新调整"),
            ("Wing_Work", "项目推进时更重视稳定性"),
            ("Wing_Emotion", "最近偶尔觉得焦虑"),
            ("Wing_Emotion", "散步后情绪会平静一些"),
        ]
        for wing, text in facts:
            await nats_publish_assertion(
                handle.nats_url,
                user_id=handle.user_id,
                text=text,
                wing=wing,
                memory_type="profile",
            )

        async with mcp_session(handle.mcp_url) as session:

            async def _facts_landed(s) -> bool:
                payload = mcp_tool_json(
                    await s.call_tool(
                        "eidolon_memory_list",
                        {"limit": 100, "include_private": False},
                    )
                )
                return len((payload or {}).get("records") or []) >= len(facts)

            assert await wait_for_visible(session, predicate=_facts_landed, timeout_s=30)
            log_path = tmp_path / "bounded-consolidator.log"
            started = time.monotonic()
            proc = _run_consolidator(
                user_id=handle.user_id,
                settings_yaml=handle.settings_path,
                log_path=log_path,
                timeout_s=20,
                synthesis_budget_s=8.0,
                max_parallel_wings=2,
            )
            elapsed = time.monotonic() - started

            diagnostic = (
                f"requests={completion_server.request_count} "
                f"paths={completion_server.request_paths}\n"
                f"{tail_file(log_path, max_chars=5000)}"
            )
            assert proc.returncode == 0, diagnostic
            assert elapsed < 15
            output = log_path.read_text(encoding="utf-8")
            assert '"status": "partial"' in output, diagnostic
            assert '"themes_published": 1' in output, diagnostic
            assert '"status": "timed_out"' in output, diagnostic

            async def _theme_landed(s) -> bool:
                payload = mcp_tool_json(
                    await s.call_tool(
                        "eidolon_memory_list",
                        {"limit": 100, "include_private": False},
                    )
                )
                return any(
                    (record.get("metadata") or {}).get("wing") == "Wing_Theme"
                    for record in (payload or {}).get("records") or []
                )

            assert await wait_for_visible(session, predicate=_theme_landed, timeout_s=30)
