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
from datetime import UTC, datetime
from typing import Any

from eidolon_memory_contracts import (
    PrivacyMutationCommand,
    readable_audiences,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from eidolon.memory.adapters.kg_sqlite import now_iso as _now_iso
from eidolon.memory.application.explicit_writes import publish_with_status
from eidolon.memory.application.forget import (
    ForgetResolutionLimitExceeded,
    find_forget_candidates,
)
from eidolon.memory.application.materialization import inspect_materialization
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.application.mempalace_hierarchy import build_owner_browse
from eidolon.memory.application.owner_entries import build_owner_entries
from eidolon.memory.application.owner_export import build_owner_export
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
STATUS_PATH = "/api/memory/v1/status"
EXPORT_PATH = "/api/memory/v1/export"
ENTRIES_PATH = "/api/memory/v1/entries"
FORGET_PREVIEW_PATH = "/api/memory/v1/forget/preview"
FORGET_CONFIRM_PATH = "/api/memory/v1/forget/confirm"
GRAPH_PATH = "/api/memory/v1/graph"

#: How much of the palace one browse reads. A bound is required — the scan is a
#: full enumeration — and it is not a page: the roll-up needs the whole window to
#: count rooms honestly. Exceeding it is reported as ``truncated`` rather than
#: silently answering for part of the palace.
DEFAULT_SCAN = 4000
MAXIMUM_SCAN = 20000
#: Titles listed per room. Enough to recognise a room's contents, not enough to
#: turn a browse into a bulk export — that is what ``export`` is for.
TITLES_PER_ROOM = 12
#: How much of the palace one export reads. The same bound as a browse, because
#: it is the same full enumeration, but taken by default rather than on request:
#: a browse is a page someone is looking at, and a partial export is the failure
#: mode rather than a cheaper answer.
DEFAULT_EXPORT_SCAN = MAXIMUM_SCAN
#: Entries returned in one answer. A day's worth of memory is short; a client
#: asking for more than this is asking for the library, which has its own read.
DEFAULT_ENTRIES = 50
MAXIMUM_ENTRIES = 200
#: How long a confirm waits on the applied-projection before answering. Short:
#: the command is durably published either way, and a person watching a spinner
#: is worse served by a long wait than by "已受理，正在生效".
CONFIRM_WAIT_SECONDS = 0.75
DEFAULT_GRAPH_EDGES = 160
MAXIMUM_GRAPH_EDGES = 400


def status_handler(
    *,
    service: MemoryService,
    memory_space_id: str,
    owner_id: str | None = None,
) -> Handler:
    """Return the same storage-backed status every Host consumer sees."""

    async def handle(request: Request) -> Response:
        companion_id = (request.query_params.get("companion_id") or "").strip() or None
        context = actor_context(
            memory_space_id=memory_space_id,
            owner_id=owner_id,
            companion_id=companion_id,
        )
        try:
            status = await service.status(context)
        except Exception as exc:  # noqa: BLE001 - status is a failure boundary
            log.exception(
                "owner_memory_status_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
            )
            return JSONResponse({"detail": "memory is unavailable"}, status_code=503)
        return JSONResponse(
            {
                "contract_version": "1",
                "operation": "memory.status",
                "memory_realm_id": memory_space_id,
                "memory_space_id": memory_space_id,
                "audience_scope": (
                    f"companion:{companion_id}" if companion_id else "owner"
                ),
                "ready": status.ready,
                **status.details,
            }
        )

    return handle


def graph_handler(
    *,
    service: MemoryService,
    settings: MemorySettings,
    memory_space_id: str,
    owner_id: str | None = None,
) -> Handler:
    """A bounded, audience-scoped knowledge graph for the Owner's viewer."""

    del settings

    async def handle(request: Request) -> Response:
        companion_id = (request.query_params.get("companion_id") or "").strip() or None
        try:
            limit = int(request.query_params.get("limit", DEFAULT_GRAPH_EDGES))
        except ValueError:
            return JSONResponse({"detail": "limit must be a number"}, status_code=422)
        limit = max(1, min(limit, MAXIMUM_GRAPH_EDGES))
        context = actor_context(
            memory_space_id=memory_space_id,
            owner_id=owner_id,
            companion_id=companion_id,
        )
        try:
            runtime = await service.runtime_for(context)
            if runtime.kg is None:
                return JSONResponse(
                    {
                        "contract_version": "1",
                        "operation": "memory.graph",
                        "memory_space_id": memory_space_id,
                        "nodes": [],
                        "edges": [],
                        "truncated": False,
                    }
                )
            rows = await runtime.kg.timeline(
                audiences=readable_audiences(companion_id),
                limit=limit + 1,
                current_only=True,
                include_sensitive=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "owner_graph_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
            )
            return JSONResponse({"detail": "memory graph is unavailable"}, status_code=503)

        visible = rows[:limit]
        degree: dict[str, int] = {}
        for row in visible:
            degree[row.subject] = degree.get(row.subject, 0) + 1
            degree[row.object] = degree.get(row.object, 0) + 1
        return JSONResponse(
            {
                "contract_version": "1",
                "operation": "memory.graph",
                "memory_space_id": memory_space_id,
                "nodes": [
                    {"node_id": name, "label": name, "degree": count}
                    for name, count in sorted(
                        degree.items(), key=lambda item: (-item[1], item[0])
                    )
                ],
                "edges": [
                    {
                        "edge_id": row.id,
                        "subject": row.subject,
                        "predicate": row.predicate,
                        "object": row.object,
                        "confidence": row.confidence,
                        "recorded_at": row.recorded_at or "",
                    }
                    for row in visible
                ],
                "truncated": len(rows) > limit,
            }
        )

    return handle


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

    ``companion_id`` selects one Companion view. Without it this authenticated
    route shows Owner Shared only. This is an explicit privileged read mode,
    never an identity fallback on the ordinary Agent MCP surface.
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
                visible=lambda record: policy.visible(
                    record,
                    context=context,
                    owner_shared_only=companion_id is None,
                ),
                max_records=scan,
                max_titles_per_room=TITLES_PER_ROOM,
            )
            materialization = await inspect_materialization(runtime)
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
                "audience_scope": (
                    f"companion:{companion_id}" if companion_id else "owner"
                ),
                "materialization": {
                    "ready": materialization.ready,
                    **materialization.details,
                },
                **browse,
            }
        )

    return handle


def export_handler(
    *,
    service: MemoryService,
    settings: MemorySettings,
    memory_space_id: str,
    owner_id: str | None = None,
) -> Handler:
    """A copy of this memory the person can read and keep.

    The other three reads are pages: they shorten, roll up, and page, because
    someone is looking at them. This one is a file, so it carries statements
    whole and puts the ones it cannot date at the end instead of leaving them
    out — an export that omitted something would be a copy that quietly is not
    one.

    Deliberately not the Host backup. That copy is the palace itself and exists
    so a lost disk is survivable; this one exists so a person is not locked in,
    and the two have almost nothing in common but the word.

    ``companion_id`` selects an audience exactly as the browse does — with one
    difference that only this route has: **without it, this is the Owner asking
    for their own copy**, so it carries every audience in their Realm and names
    the audience on each record. A memory somebody marked 「只让它记得」 is still
    theirs; leaving it out of the file they saved would be losing it. Named
    with one, the export is "what this Eidolon can recall" and keeps the recall
    predicate exactly.
    """

    policy = RecallPolicyRegistry.default()

    async def handle(request: Request) -> Response:
        companion_id = (request.query_params.get("companion_id") or "").strip() or None
        context = actor_context(
            memory_space_id=memory_space_id,
            owner_id=owner_id,
            companion_id=companion_id,
        )
        try:
            runtime = await service.runtime_for(context)
            export = await build_owner_export(
                runtime.backend,
                visible=lambda record: policy.visible(
                    record,
                    context=context,
                    every_audience=companion_id is None,
                ),
                max_records=DEFAULT_EXPORT_SCAN,
            )
        except Exception as exc:  # noqa: BLE001 - a read must not take the process down
            log.exception(
                "owner_export_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
            )
            # Not an empty file: a person who saved one would believe their
            # Eidolon remembers nothing.
            return JSONResponse({"detail": "memory is unavailable"}, status_code=503)

        return JSONResponse(
            {
                "contract_version": "1",
                "operation": "memory.export",
                "memory_space_id": memory_space_id,
                "taken_at": datetime.now(UTC).isoformat(),
                **export,
            }
        )

    return handle


def entries_handler(
    *,
    service: MemoryService,
    settings: MemorySettings,
    memory_space_id: str,
    owner_id: str | None = None,
) -> Handler:
    """What was recorded at or after ``since``, newest first.

    ``since`` is required and has no default. A day depends on where the person
    is, and this process does not know; inventing a timezone here would make
    "今日" mean something different from what their phone shows them.
    """

    policy = RecallPolicyRegistry.default()

    async def handle(request: Request) -> Response:
        raw_since = (request.query_params.get("since") or "").strip()
        if not raw_since:
            return JSONResponse({"detail": "since is required"}, status_code=422)
        try:
            since = datetime.fromisoformat(raw_since)
        except ValueError:
            # A "+" in a query string means a space, so an unencoded offset
            # arrives here mangled. Saying so turns a confusing afternoon into
            # a one-line fix; the alternative — repairing it — would be this
            # boundary guessing at a caller's encoding.
            hint = (
                " (an unencoded + in the offset arrives as a space)"
                if " " in raw_since
                else ""
            )
            return JSONResponse(
                {"detail": f"since must be an ISO 8601 instant{hint}"},
                status_code=422,
            )
        if since.tzinfo is None:
            # A naive instant would be compared against timezone-aware record
            # times and raise; asking for the offset is better than guessing UTC
            # and answering for the wrong day.
            return JSONResponse(
                {"detail": "since must carry a timezone offset"}, status_code=422
            )
        try:
            limit = int(request.query_params.get("limit", DEFAULT_ENTRIES))
        except ValueError:
            return JSONResponse({"detail": "limit must be a number"}, status_code=422)
        limit = max(1, min(limit, MAXIMUM_ENTRIES))
        companion_id = (request.query_params.get("companion_id") or "").strip() or None

        context = actor_context(
            memory_space_id=memory_space_id,
            owner_id=owner_id,
            companion_id=companion_id,
        )
        try:
            runtime = await service.runtime_for(context)
            entries = await build_owner_entries(
                runtime.backend,
                visible=lambda record: policy.visible(
                    record,
                    context=context,
                    owner_shared_only=companion_id is None,
                ),
                since=since,
                limit=limit,
                max_records=DEFAULT_SCAN,
            )
        except Exception as exc:  # noqa: BLE001 - a read must not take the process down
            log.exception(
                "owner_entries_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
            )
            return JSONResponse({"detail": "memory is unavailable"}, status_code=503)

        return JSONResponse(
            {
                "contract_version": "1",
                "operation": "memory.entries",
                "memory_space_id": memory_space_id,
                "since": since.isoformat(),
                **entries,
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
        STATUS_PATH: (
            status_handler(
                service=service,
                memory_space_id=memory_space_id,
                owner_id=owner_id,
            ),
            ["GET"],
        ),
        RECOLLECTIONS_PATH: (recollections_handler(**shared), ["GET"]),
        BROWSE_PATH: (browse_handler(**shared), ["GET"]),
        EXPORT_PATH: (export_handler(**shared), ["GET"]),
        ENTRIES_PATH: (entries_handler(**shared), ["GET"]),
        GRAPH_PATH: (graph_handler(**shared), ["GET"]),
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
