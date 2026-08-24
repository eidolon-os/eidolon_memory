"""The Owner-facing memory surface: ``/api/memory/v1/*``, credential-gated.

One factory builds every route in this family, and it wraps each of them in the
service-credential check. That shape is the point: a route added here cannot be
added without the guard, because the guard is applied by the thing that mounts
it rather than by each handler remembering to call something.

Why it matters more here than almost anywhere: the surface is about to carry
``forget``. A destructive operation on a person's memory, on an unauthenticated
port, is the kind of thing that is obvious in hindsight and invisible while it
is being built one endpoint at a time.

What this is *not*: authentication of the Owner. This is a service credential —
proof that the caller is the Host's own Admin, which has already authenticated a
Controller and decided whose memory this is. This process serves one space per
port and cannot tell one person from another; it can only tell "the Host asked"
from "something else on this machine asked", and that is the question it should
be answering.

The MCP transports on the same app are deliberately untouched. The agent speaks
to them, and adding a credential there is a separate change with a separate
consumer — mixing it into this one would mean the agent stops talking to its own
memory the moment this lands.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

#: Everything under here answers only to the Host's own service credential.
API_PREFIX = "/api/memory/v1"

Handler = Callable[[Request], Awaitable[Response]]


def _guarded(handler: Handler, *, expected_token: str) -> Handler:
    """Admit only a caller presenting this Host's memory service credential.

    Fails closed when the credential is unconfigured. A surface with no
    credential to check is not an open surface, it is one that cannot answer —
    503 rather than 401, because the caller has nothing to fix.
    """

    async def guarded(request: Request) -> Response:
        token = expected_token.strip()
        if not token:
            return JSONResponse(
                {"detail": "memory service credential is not configured"},
                status_code=503,
            )
        scheme, separator, presented = (
            request.headers.get("authorization") or ""
        ).partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(presented, token)
        ):
            return JSONResponse(
                {"detail": "memory service authentication failed"},
                status_code=401,
            )
        return await handler(request)

    return guarded


def memory_api_routes(
    *,
    service_token: str,
    routes: dict[str, tuple[Handler, list[str]]],
) -> list[Route]:
    """Mount the Owner-facing memory routes, every one of them gated.

    ``routes`` maps a path under :data:`API_PREFIX` to its handler and methods.
    Passing them through here rather than constructing ``Route`` objects at each
    call site is what makes "forgot the credential" unrepresentable.
    """

    mounted: list[Route] = []
    for path, (handler, methods) in routes.items():
        if not path.startswith(API_PREFIX):
            raise ValueError(
                f"{path} is outside {API_PREFIX}; this factory only gates that family"
            )
        mounted.append(
            Route(path, _guarded(handler, expected_token=service_token), methods=methods)
        )
    return mounted


def actor_context(
    *,
    memory_space_id: str,
    owner_id: str | None,
    companion_id: str | None,
) -> Any:
    """Who is asking, for a space this process already serves.

    Lives here because every route in this family needs it and none of them
    should build it differently. ``memory_space_id`` is this process's, never a
    caller's — the route carries no realm, and letting a query name one would
    make this surface answer for a space its caller was never routed to.

    ``companion_id`` selects an *audience*: absent is the Owner layer, present
    adds that Companion's own. It cannot widen what the space can see.
    """

    from eidolon_memory_contracts import MemoryActorContext

    return MemoryActorContext(
        owner_id=owner_id,
        companion_id=companion_id,
        memory_realm_id=memory_space_id,
        memory_space_id=memory_space_id,
    )
