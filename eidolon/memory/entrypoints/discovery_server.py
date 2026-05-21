"""Standalone Discovery HTTP server for eidolon-agent routing."""

from __future__ import annotations

import argparse
import json
from collections.abc import Awaitable, Callable
from typing import Any

from eidolon.memory.application.discovery import build_agent_routing_discovery
from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _send_json(
    send: AsgiSend,
    status: int,
    payload: dict[str, Any],
) -> None:
    body = _json_bytes(payload)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class DiscoveryApp:
    """Minimal ASGI app; avoids coupling discovery to Admin/FastAPI."""

    def __init__(self, settings: MemorySettings | None = None) -> None:
        self._settings = settings

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        if scope["type"] != "http":
            await _send_json(send, 404, {"detail": "not found"})
            return

        settings = self._settings or get_memory_settings()
        route_path = settings.discovery_http.path
        if not route_path.startswith("/"):
            route_path = f"/{route_path}"

        if scope.get("method") != "GET" or scope.get("path") != route_path:
            await _send_json(send, 404, {"detail": "not found"})
            return

        try:
            payload = await build_agent_routing_discovery(settings)
        except Exception as exc:
            log.exception("discovery_agent_routing_failed", error=str(exc))
            await _send_json(send, 500, {"detail": "discovery unavailable"})
            return
        await _send_json(send, 200, payload)


app = DiscoveryApp()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_memory_settings()
    parser = argparse.ArgumentParser(
        prog="eidolon-memory-discovery",
        description="Standalone Eidolon Memory Discovery HTTP server.",
    )
    parser.add_argument("--host", default=settings.discovery_http.host)
    parser.add_argument("--port", type=int, default=settings.discovery_http.port)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = _parse_args(argv)
    log.info("discovery_server_start", host=args.host, port=args.port)
    uvicorn.run(
        "eidolon.memory.entrypoints.discovery_server:app",
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
