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

import uuid
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from eidolon_memory_contracts import PrivacyMutationCommand

from eidolon.memory.adapters.kg_sqlite import now_iso as _now_iso
from eidolon.memory.application.explicit_writes import publish_with_status
from eidolon.memory.application.forget import (
    ForgetResolutionLimitExceeded,
    find_forget_candidates,
)
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
FORGET_PREVIEW_PATH = "/api/memory/v1/forget/preview"
FORGET_CONFIRM_PATH = "/api/memory/v1/forget/confirm"

#: How much of the palace one browse reads. A bound is required — the scan is a
#: full enumeration — and it is not a page: the roll-up needs the whole window to
#: count rooms honestly. Exceeding it is reported as ``truncated`` rather than
#: silently answering for part of the palace.
DEFAULT_SCAN = 4000
MAXIMUM_SCAN = 20000
#: Titles listed per room. Enough to recognise a room's contents, not enough to
#: turn a browse into a bulk export — that is what ``export`` will be for.
TITLES_PER_ROOM = 12
#: How long a confirm waits on the applied-projection before answering. Short:
#: the command is durably published either way, and a person watching a spinner
#: is worse served by a long wait than by "已受理，正在生效".
CONFIRM_WAIT_SECONDS = 0.75


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


def forget_preview_handler(
    *,
    service: MemoryService,
    memory_space_id: str,
    owner_id: str | None = None,
) -> Handler:
    """What "forget this" would remove, resolved and shown before anything moves.

    A preview, not a dry run of a delete: nothing is written, and what comes
    back is the exact set the confirm will act on, bound into a signed token.
    That binding is the point — a person confirms *what they saw*, not a topic
    that may have matched something else by the time they pressed the button.

    No audience filter, deliberately. The actor here is the Owner and the space
    is the Owner's; filtering by a Companion's audience would leave a person
    unable to remove something they had asked one Eidolon in particular to keep,
    which is a trap rather than a protection. What the preview lists is what
    their own words matched.
    """

    async def handle(request: Request) -> Response:
        target = (request.query_params.get("target") or "").strip()
        if not target:
            return JSONResponse({"detail": "target is required"}, status_code=422)
        action = (request.query_params.get("action") or "delete").strip()
        if action not in {"archive", "delete"}:
            return JSONResponse(
                {"detail": "action must be archive or delete"}, status_code=422
            )

        context = actor_context(
            memory_space_id=memory_space_id,
            owner_id=owner_id,
            companion_id=None,
        )
        try:
            runtime = await service.runtime_for(context)
            candidates = await find_forget_candidates(
                runtime.backend, memory_space_id, target
            )
        except ForgetResolutionLimitExceeded as exc:
            # Too many matches to show, so nothing is offered to confirm. A
            # partial set would be the one outcome worse than refusing: the
            # person would believe the rest was kept when it was merely unseen.
            return JSONResponse(
                {
                    "contract_version": "1",
                    "operation": "memory.forget-preview",
                    "status": "too_broad",
                    "target": target,
                    "detail": str(exc),
                },
                status_code=200,
            )
        except Exception as exc:  # noqa: BLE001 - a read must not take the process down
            log.exception(
                "owner_forget_preview_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
            )
            return JSONResponse({"detail": "memory is unavailable"}, status_code=503)

        if not candidates:
            # No token: there is nothing to confirm, and issuing one anyway
            # would let a person press a button that deletes nothing and says
            # it worked.
            return JSONResponse(
                {
                    "contract_version": "1",
                    "operation": "memory.forget-preview",
                    "status": "not_found",
                    "target": target,
                    "entries": [],
                }
            )

        token, proof = service.privacy_signer.issue(
            memory_space_id=memory_space_id,
            action=action,  # type: ignore[arg-type]
            target=target,
            drawer_ids=[candidate.key for candidate in candidates],
        )
        ambiguous = len(candidates) > 1 or any(
            candidate.score < 1.0 for candidate in candidates
        )
        return JSONResponse(
            {
                "contract_version": "1",
                "operation": "memory.forget-preview",
                "status": "preview",
                "target": target,
                "action": action,
                "entries": [candidate.to_dict() for candidate in candidates],
                #: True when the match was not exact or hit more than one thing.
                #: A client must ask again in that case rather than treating a
                #: guess as an instruction.
                "needs_confirmation": action == "delete" and ambiguous,
                "confirmation_token": token,
                "expires_at": proof.expires_at,
            }
        )

    return handle


def forget_confirm_handler(
    *,
    service: MemoryService,
    memory_space_id: str,
    command_publisher: Any,
    command_status: Any,
    owner_id: str | None = None,
) -> Handler:
    """Apply exactly the set a preview showed, or refuse.

    The token is the whole safety property: it carries the space, the action and
    the exact drawer ids, and it is verified against the same signer that minted
    it. A confirm that arrived without one — or with one from another space, or
    expired — is refused rather than re-resolved, because re-resolving would act
    on whatever the topic matches *now*.
    """

    async def handle(request: Request) -> Response:
        token = (request.query_params.get("confirmation_token") or "").strip()
        if not token:
            return JSONResponse(
                {"detail": "confirmation_token is required"}, status_code=422
            )
        try:
            proof = service.privacy_signer.verify(
                token, expected_memory_space_id=memory_space_id
            )
        except ValueError as exc:
            # Forged, expired, or minted for another space. All three are the
            # same answer to the caller: this token cannot be acted on.
            return JSONResponse({"detail": str(exc)}, status_code=409)

        command = PrivacyMutationCommand(
            request_id=uuid.uuid4().hex,
            memory_space_id=memory_space_id,
            issued_at=_now_iso(),
            #: ``admin``, not ``agent``. The contract's two values already carry
            #: the distinction that matters to someone reading this ledger later:
            #: whether the Eidolon decided on its own, or a person asked through
            #: a management surface. A third value for "the Owner in particular"
            #: would be a cross-repo contract change for a nuance these two
            #: already express.
            issuer="admin",
            action=proof.action,
            drawer_ids=proof.drawer_ids,
            preview_id=proof.preview_id,
            target=proof.target,
        )
        try:
            outcome = await publish_with_status(
                command_publisher,
                command_status,
                command,
                wait_seconds=CONFIRM_WAIT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - a write must not take the process down
            log.exception(
                "owner_forget_confirm_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
            )
            return JSONResponse({"detail": "memory is unavailable"}, status_code=503)

        return JSONResponse(
            {
                "contract_version": "1",
                "operation": "memory.forget-confirm",
                "action": proof.action,
                "target": proof.target,
                "entry_count": len(proof.drawer_ids),
                **outcome,
            }
        )

    return handle


def owner_memory_routes(
    *,
    service: MemoryService,
    settings: MemorySettings,
    memory_space_id: str,
    service_token: str,
    command_publisher: Any = None,
    command_status: Any = None,
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
    routes: dict[str, tuple[Handler, list[str]]] = {
        RECOLLECTIONS_PATH: (recollections_handler(**shared), ["GET"]),
        BROWSE_PATH: (browse_handler(**shared), ["GET"]),
        FORGET_PREVIEW_PATH: (
            forget_preview_handler(
                service=service, memory_space_id=memory_space_id, owner_id=owner_id
            ),
            ["POST"],
        ),
    }
    if command_publisher is not None:
        # Only mounted where the mutation can actually be published. A confirm
        # route that always failed would be a button this Host promises and
        # cannot honour; its absence is discoverable, and the preview above
        # stays useful for seeing what would go.
        routes[FORGET_CONFIRM_PATH] = (
            forget_confirm_handler(
                service=service,
                memory_space_id=memory_space_id,
                command_publisher=command_publisher,
                command_status=command_status,
                owner_id=owner_id,
            ),
            ["POST"],
        )
    return memory_api_routes(service_token=service_token, routes=routes)
