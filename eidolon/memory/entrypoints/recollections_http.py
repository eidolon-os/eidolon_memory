"""A plain HTTP read surface for "what do you remember about this".

Why this exists beside the MCP tools that already answer it: the tools are
reachable by callers that speak MCP, and the Eidolon's agent is one. The person
who owns the Eidolon is not — the phone in their hand reaches its Host over the
Local API, and Admin projects that boundary from ordinary HTTP services. Until
now the memory a person owns had no shape anywhere in the product except an
identifier on a card.

It is deliberately the *search* contract and not recall: a person asking what
their Eidolon remembers wants what is stored, not what would be relevant to a
conversational turn. Both go through the same service and the same policy, so
what a person is shown can never be more than what the agent could have seen.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.application.public_recall import (
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.entrypoints.memory_api import Handler, actor_context
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

#: This process serves one space per port, so the route carries no realm. The
#: caller reaching it has already been routed to the right one by discovery,
#: and letting a query name a different realm would make this surface answer
#: for a space its caller was never routed to.
RECOLLECTIONS_PATH = "/api/memory/v1/recollections"

MAXIMUM_RESULTS = 50
DEFAULT_RESULTS = 10


def recollections_handler(
    *,
    service: MemoryService,
    settings: MemorySettings,
    memory_space_id: str,
    owner_id: str | None = None,
) -> Handler:
    """A GET returning what this space holds about a query.

    ``companion_id`` is a query parameter rather than an argument here, because
    the space belongs to the Owner and serves every Companion that Owner has.
    It selects an *audience*, not a scope: without it the answer is the owner
    layer, with it the owner layer plus that Companion's own. It cannot widen
    what this space can see, so the caller naming it is not naming a scope it
    was not routed to.
    """

    async def handle(request: Request) -> JSONResponse:
        query = (request.query_params.get("q") or "").strip()
        companion_id = (request.query_params.get("companion_id") or "").strip() or None
        if not query:
            return JSONResponse(
                {"detail": "q is required"},
                status_code=422,
            )
        try:
            limit = int(request.query_params.get("limit", DEFAULT_RESULTS))
        except ValueError:
            return JSONResponse({"detail": "limit must be a number"}, status_code=422)
        limit = max(1, min(limit, MAXIMUM_RESULTS))

        context = actor_context(
            memory_space_id=memory_space_id,
            owner_id=owner_id,
            companion_id=companion_id,
        )
        try:
            runtime = await service.runtime_for(context)
            records = await search_all_wings_mcp_style(
                runtime.backend,
                settings,
                query=query,
                context=context,
                top_k=limit,
                wing=None,
                room=None,
                for_voice=False,
                palace_path=runtime.palace_path,
                # A search that could not run must not come back as a search
                # that found nothing. The default here is to degrade quietly,
                # which is right for a conversation — an Eidolon that cannot
                # reach its memory should still answer the person in front of
                # it. It is wrong for someone who asked, in as many words,
                # what their Eidolon remembers: the honest answer to that is
                # "I could not look", and it is a different answer from "there
                # is nothing".
                raise_on_degraded=True,
            )
        except Exception as exc:  # noqa: BLE001 - a read must not take the process down
            log.exception(
                "recollections_search_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
            )
            return JSONResponse(
                {"detail": "memory is unavailable"},
                status_code=503,
            )
        return JSONResponse(
            {
                "operation": "memory.recollections",
                "contract_version": "1",
                "memory_space_id": memory_space_id,
                "query": query,
                "recollections": [
                    wire_record_to_public_dict(record) for record in records
                ],
            }
        )

    return handle
