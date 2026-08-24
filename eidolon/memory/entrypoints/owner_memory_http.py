"""Every Owner-facing memory route, mounted in one place.

One function builds them all and passes them through the credential factory, so
there is a single path from "a handler exists" to "it is reachable". The
alternative — each module mounting its own — is how a surface ends up with one
route gated and the next one not, and how a test ends up exercising a mounting
path the process does not use.

What belongs here: reads and writes a *person* makes about their own memory.
What does not: the operator and agent surfaces. Those are the MCP transports on
the same app, they answer different questions with different vocabulary, and one
of them hands out filesystem paths.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from eidolon.memory.application.mempalace_hierarchy import build_owner_browse
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.entrypoints.memory_api import (
    Handler,
    actor_context,
    memory_api_routes,
)
from eidolon.memory.entrypoints.recollections_http import (
    RECOLLECTIONS_PATH,
    recollections_handler,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

BROWSE_PATH = "/api/memory/v1/browse"

#: How much of the palace one browse reads. A bound is required — the scan is a
#: full enumeration — and it is not a page: the roll-up needs the whole window to
#: count rooms honestly. Exceeding it is reported as ``truncated`` rather than
#: silently answering for part of the palace.
DEFAULT_SCAN = 4000
MAXIMUM_SCAN = 20000
#: Titles listed per room. Enough to recognise a room's contents, not enough to
#: turn a browse into a bulk export — that is what ``export`` will be for.
TITLES_PER_ROOM = 12


def browse_handler(
    *,
    service: MemoryService,
    settings: MemorySettings,
    memory_space_id: str,
    owner_id: str | None = None,
) -> Handler:
    """What this memory holds, by wing and room.

    The same visibility policy recall uses decides what appears, passed in as a
    predicate. A person cannot be shown something their Eidolon could not have
    recalled — including their own privacy wing, and (once anything is marked
    companion-private) another Companion's statements.

    ``companion_id`` selects an audience exactly as it does for recollections:
    absent means the Owner layer, present adds that Companion's own. It cannot
    widen what this space can see.
    """

    policy = RecallPolicyRegistry.default()

    async def handle(request: Request) -> Response:
        companion_id = (request.query_params.get("companion_id") or "").strip() or None
        try:
            scan = int(request.query_params.get("max_records", DEFAULT_SCAN))
        except ValueError:
            return JSONResponse(
                {"detail": "max_records must be a number"}, status_code=422
            )
        scan = max(1, min(scan, MAXIMUM_SCAN))

        context = actor_context(
            memory_space_id=memory_space_id,
            owner_id=owner_id,
            companion_id=companion_id,
        )
        try:
            runtime = await service.runtime_for(context)
            browse = await build_owner_browse(
                runtime.backend,
                settings,
                visible=lambda record: policy.visible(record, context=context),
                max_records=scan,
                max_titles_per_room=TITLES_PER_ROOM,
            )
        except Exception as exc:  # noqa: BLE001 - a read must not take the process down
            log.exception(
                "owner_browse_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
            )
            # An empty palace and a palace that could not be read look identical
            # to a person, and only one of them is a reason to worry.
            return JSONResponse({"detail": "memory is unavailable"}, status_code=503)

        return JSONResponse(
            {
                "contract_version": "1",
                "operation": "memory.browse",
                "memory_space_id": memory_space_id,
                **browse,
            }
        )

    return handle


def owner_memory_routes(
    *,
    service: MemoryService,
    settings: MemorySettings,
    memory_space_id: str,
    service_token: str,
    owner_id: str | None = None,
) -> list[Route]:
    """The whole Owner-facing family, credential-gated.

    ``service_token`` is required rather than defaulted: a default would make
    "nobody passed one" indistinguishable from "this Host has none", and the
    first is a wiring mistake while the second is a Host that cannot answer.
    """

    shared = {
        "service": service,
        "settings": settings,
        "memory_space_id": memory_space_id,
        "owner_id": owner_id,
    }
    return memory_api_routes(
        service_token=service_token,
        routes={
            RECOLLECTIONS_PATH: (recollections_handler(**shared), ["GET"]),
            BROWSE_PATH: (browse_handler(**shared), ["GET"]),
        },
    )
