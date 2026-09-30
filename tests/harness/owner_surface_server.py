"""A memory realm's Owner surface, served for real, for another repository's tests.

Admin's end-to-end test spawns this in this repository's own environment and
talks to it over loopback, the way a Host's Admin talks to a realm. Every route
is the production handler from ``owner_memory_http`` behind the production
credential gate, answering through the shared Owner contract; the signer and
the command ledger are the production classes. Only two things are stand-ins:

- storage is the fake backend, seeded with a small memory spread across the
  Owner layer, two Companions, and one drawer that has no ledger assertion;
- the worker is a publisher that applies a forget after a short delay — removes
  the drawers and records ``applied`` in the real ledger — so a client sees
  ``accepted`` first and has to ask again, as it does on a Host.

Discovery is served on the same port, as the one realm this Owner has.

Run: ``python tests/harness/owner_surface_server.py --port P --token T --state DIR``.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn
from eidolon_memory_contracts import OWNER_AUDIENCE, PrivacyMutationCommand, companion_audience
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.privacy_confirmation import PrivacyConfirmationSigner
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.entrypoints.owner_memory_http import owner_memory_routes
from eidolon.memory.infrastructure.command_status import CommandStatusLedger

SPACE = "r_owner_one"
OWNER = "owner-1"
NOON = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

#: (key, text, audience, canonical, minutes before noon)
SEED: tuple[tuple[str, str, str, bool, int], ...] = (
    ("drawer_salary", "工资是两万", OWNER_AUDIENCE, True, 0),
    ("drawer_salary_a", "工资涨到三万了", companion_audience("c_a"), True, 1),
    ("drawer_tea_b", "喜欢乌龙茶", companion_audience("c_b"), True, 2),
    ("drawer_legacy", "工资的旧记录", OWNER_AUDIENCE, False, 3),
    *(
        (f"drawer_walk_{index}", f"下午散步第{index}次", companion_audience("c_a"), True, 10)
        for index in range(4)
    ),
)


class _Service:
    def __init__(self, backend: FakeMemoryBackend) -> None:
        self.privacy_signer = PrivacyConfirmationSigner()
        self._runtime = SimpleNamespace(
            space_id=SPACE,
            backend=backend,
            palace_path="/tmp/palace",
            kg=None,
            ledgers=SimpleNamespace(canonical_facts=None, commitments=None),
        )

    async def runtime_for(self, context: Any) -> SimpleNamespace:
        return self._runtime


class _ApplyingPublisher:
    """Stands in for the worker: applies a forget a moment after accepting it."""

    def __init__(self, backend: FakeMemoryBackend, ledger: CommandStatusLedger) -> None:
        self._backend = backend
        self._ledger = ledger
        self._tasks: set[asyncio.Task[None]] = set()

    async def publish(self, command: PrivacyMutationCommand) -> None:
        task = asyncio.create_task(self._apply(command))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _apply(self, command: PrivacyMutationCommand) -> None:
        # Longer than the confirm's own wait, so the first answer is `accepted`
        # and the client has to ask again — the usual case on a Host.
        await asyncio.sleep(1.5)
        for key in command.drawer_ids:
            self._backend.docs.pop(f"{SPACE}::{key}", None)
        await self._ledger.record_applied(command.request_id, kind=command.kind)


async def _seed(backend: FakeMemoryBackend) -> None:
    for key, text, audience, canonical, minutes in SEED:
        metadata: dict[str, Any] = {
            "memory_space_id": SPACE,
            "audience": audience,
            "occurred_at": (NOON - timedelta(minutes=minutes)).isoformat(),
        }
        if canonical:
            metadata["assertion_id"] = f"assert-{key}"
        await backend.ingest_text(wing="Wing_Life", room=key, text=text, metadata=metadata)


def build(port: int, token: str, state: Path) -> Starlette:
    backend = FakeMemoryBackend()
    asyncio.run(_seed(backend))
    ledger = CommandStatusLedger(state / "cmd.sqlite3", space_id=SPACE)

    async def discovery(_request: Any) -> JSONResponse:
        return JSONResponse(
            {
                "memory_realms": [
                    {
                        "memory_realm_id": SPACE,
                        "memory_space_id": SPACE,
                        "owner_id": OWNER,
                        "recollections_url": f"http://127.0.0.1:{port}/api/memory/v1/recollections",
                        "enabled": True,
                        "agent_reachable": True,
                    }
                ]
            }
        )

    return Starlette(
        routes=[
            Route("/api/discovery/agent-routing", discovery, methods=["GET"]),
            *owner_memory_routes(
                service=_Service(backend),  # type: ignore[arg-type]
                settings=load_memory_settings(),
                memory_space_id=SPACE,
                owner_id=OWNER,
                service_token=token,
                command_publisher=_ApplyingPublisher(backend, ledger),
                command_status=ledger,
            ),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    app = build(args.port, args.token, args.state)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
