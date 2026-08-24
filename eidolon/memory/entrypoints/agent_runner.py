"""Serve one memory space: MCP control plane, JetStream subscriber, steward.

The process still serves a single space, but it is no longer *defined* by one.
Opening a space — claiming it, checking it, opening its storage — belongs to a
:class:`~eidolon.memory.domain.space_runtime.MemorySpaceRouter`, which this
entrypoint restricts to the one space it was asked for. That is what allows a
future runner to serve several without any of the logic below changing, and it is
why the embedding model stops being a per-space cost.

What this process does own:

* the FastMCP control plane on its configured port (Admin / Claude IDE)
* the JetStream subscriber for ``eidolon.memory.{turn,cmd,sync}.<token>``
* the steward, in-process — writes never leave here

LiveKit pipelines in the same process call the recall path directly via
``LiveKitRecallService``, sharing the space's lock through the same backend.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from eidolon_memory_contracts import (
    conversation_turn_subject,
    memory_command_subject,
    memory_sync_subject,
)

from eidolon.memory.adapters.space_routing import build_space_router
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.application.privacy_filter import row_visible_to_listing
from eidolon.memory.application.public_recall import wire_record_to_public_dict
from eidolon.memory.application.runtime_warm import warm_read_path
from eidolon.memory.application.steward import create_steward
from eidolon.memory.application.turn_processor import (
    process_command_message,
    process_sync_message,
    process_turn_message,
)
from eidolon.memory.config.memory_settings import (
    MemorySettings,
    get_memory_settings,
)
from eidolon.memory.config.palace_directory import (
    LEDGERS_DIR_SUFFIX,
    resolve_palaces_root,
    validate_memory_space_id,
)
from eidolon.memory.entrypoints.recollections_http import recollections_route
from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.chroma_refresh import checkpoint_sqlite_wal
from eidolon.memory.infrastructure.command_status import CommandStatusLedger
from eidolon.memory.infrastructure.commitments import CommitmentLedger
from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env
from eidolon.memory.infrastructure.dlq import DlqLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger
from eidolon.memory.infrastructure.integrity import (
    fsync_directory,
)
from eidolon.memory.infrastructure.mempalace_backend import (
    apply_mempalace_backend_env,
    selected_mempalace_backend,
)
from eidolon.memory.infrastructure.nats.commands import JetStreamCommandPublisher
from eidolon.memory.infrastructure.nats.names import memory_consumer_name
from eidolon.memory.infrastructure.nats.query import memory_list_drawers_query_subject
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream
from eidolon.memory.infrastructure.process_temp import configure_process_temp_root
from eidolon.memory.support import metrics
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

#: How often the graph's size is sampled for ``/metrics``.
#:
#: A minute, because the thing being watched moves over weeks. It is a floor on
#: how stale a reading can be, not a scrape interval — Prometheus can ask as often
#: as it likes and will get the last sample without touching SQLite.
GRAPH_SAMPLE_SECONDS = 60.0


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


async def publish_graph_size(kg: Any, *, memory_space_id: str) -> None:
    """Report how big the graph has become.

    Not at scrape time: ``stats()`` runs four counting queries, a Prometheus
    endpoint that touches the database is one an operator can accidentally turn
    into load, and scrape intervals are not ours to bound. Sampled on a slow
    tick instead, so the reading is cheap to serve and bounded in staleness.

    Never called while the space lock is held. ``stats()`` takes the reader side
    and ``SpaceLock`` is not reentrant, so a writer awaiting its own reader would
    deadlock the process outright — which is why this is not folded back into the
    checkpoint's critical section however convenient that looks.

    Unlabelled, because this process serves exactly one space (see ``main``).
    Whoever makes that N:1 has to add the label here, or two spaces will
    overwrite each other's readings and the series will look like noise.

    Module level rather than a closure so the behaviour can be tested without a
    NATS connection — it needs nothing from the runner but the graph.
    """

    if kg is None:
        return
    try:
        stats = await kg.stats()
    except Exception as exc:  # noqa: BLE001 - telemetry must not break the loop
        log.warning(
            "agent_runner_graph_stats_failed",
            memory_space_id=memory_space_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    metrics.GRAPH_ENTITIES.set(int(stats.get("entities") or 0))
    metrics.GRAPH_STATEMENTS.labels(state="active").set(int(stats.get("triples_active") or 0))
    metrics.GRAPH_STATEMENTS.labels(state="invalidated").set(
        int(stats.get("triples_invalidated") or 0)
    )


def _mount_metrics(app: Any) -> None:
    """Expose metrics on the port this worker already serves.

    Alongside MCP rather than on a port of its own: the supervisor knows this
    address already, so scraping needs no new discovery, and one fewer listener
    is one fewer thing bound to an interface.

    Skipped when prometheus_client is absent — a deployment that did not install
    it should not fail to start over a missing endpoint.
    """

    if not metrics.METRICS_AVAILABLE:
        log.info("metrics_endpoint_unavailable", detail="prometheus_client not installed")
        return

    from starlette.responses import Response
    from starlette.routing import Route

    async def _serve_metrics(_request: Any) -> Response:
        return Response(
            metrics.render_metrics(),
            media_type=metrics.metrics_content_type(),
        )

    app.router.routes.append(Route("/metrics", _serve_metrics, methods=["GET"]))


def _mount_ops_surface(app: Any, ops_mcp: Any, *, path: str) -> None:
    """Serve the operator tool surface beside the agent's, on one port.

    Mounted rather than given its own port so nothing about the process topology
    changes: the supervisor still spawns one child with one ``--port``, and
    discovery still hands the agent the same URL it always did. What changes is only
    which tools each path offers.

    The ASGI app is taken from the second FastMCP and mounted whole. Its session
    manager is started by the composed lifespan — an unstarted one answers every
    request with a 500, which would make the operator surface look present and
    broken rather than absent.
    """

    from starlette.routing import Mount

    mount_at = path if path.startswith("/") else f"/{path}"
    # FastMCP serves its transport at ``streamable_http_path``, already set to this
    # path, so mounting at "/" would nest it twice.
    app.router.routes.append(Mount("", app=ops_mcp.streamable_http_app()))
    log.info("agent_runner_ops_surface_mounted", path=mount_at)


async def _nats_subscriber_loop(
    *,
    memory_space_id: str,
    settings: MemorySettings,
    backend: Any,
    kg: Any,
    kg_sqlite: str,
    sync: Any,
    command_status: CommandStatusLedger,
    dlq: DlqLedger,
    decision_store: ExtractionDecisionLedger,
    canonical_facts: CanonicalFactLedger,
    commitments: CommitmentLedger,
    stop: asyncio.Event,
    ready: asyncio.Event | None = None,
) -> None:
    """In-process JetStream pull-subscriber for both turn + command subjects."""
    import nats

    durable_turn = memory_consumer_name(settings.nats.durable_prefix, memory_space_id)
    durable_cmd = memory_consumer_name(settings.nats.durable_prefix, memory_space_id, role="cmd")
    durable_sync = memory_consumer_name(settings.nats.durable_prefix, memory_space_id, role="sync")
    turn_subject = conversation_turn_subject(memory_space_id)
    cmd_subject = memory_command_subject(memory_space_id)
    sync_subject = memory_sync_subject(memory_space_id)
    query_subject = memory_list_drawers_query_subject(memory_space_id)
    ledger = sync

    steward = create_steward(settings)
    sync_every = max(1, settings.worker.sync_every_n_turns)
    writes_since_checkpoint = 0

    async def _drain(psub, handler) -> None:
        """Fetch a batch and dispatch each message.

        Idle fetch (``TimeoutError``) is swallowed here. Any OTHER exception
        (drained / dead connection, e.g. ``msg.ack()`` failing mid-batch under
        long LLM load) propagates to the outer reconnect loop, instead of the
        prior behavior where it killed the subscriber permanently and silently
        stopped all turn + cmd ingestion until process restart.
        """
        nonlocal writes_since_checkpoint
        try:
            # Batch=32: covers typical companion bursts without one slow turn
            # blocking dozens of acks. timeout=0.2s keeps the worker
            # responsive when idle (≤200ms latency to a fresh publish).
            msgs = await psub.fetch(32, timeout=0.2)
        except TimeoutError:
            return
        for msg in msgs:
            await handler(msg)
            writes_since_checkpoint += 1
            # Exit promptly on shutdown instead of finishing a full 32-message
            # batch (which, with slow LLM turns, could run for minutes past
            # stop and blow the lifespan's 5s teardown budget). Unprocessed
            # messages in this batch stay unacked and JetStream redelivers them.
            if stop.is_set():
                break

    async def _drain_forever(psub, handler) -> None:
        """Continuously drain one subject in its own task until ``stop``.

        Each subject gets a dedicated task so a *busy* subject never starves the
        others. This matters because a turn batch is processed serially and each
        turn spends ~5-8s in the steward's LLM call — which runs OUTSIDE the
        backend lock (``LockedBackend`` locks per-operation, not across a turn).
        So while a large turn backlog churns, a command (admin KG edit /
        user-confirmed fact / consolidator theme) still acquires the lock and
        applies in ~ms on the cmd task. Intra-subject order is preserved (one
        task drains its subject sequentially); there is no cross-subject order
        contract. Idle loops cheaply; a real fetch/ack error propagates so the
        outer reconnect rebinds every subscription together.
        """
        while not stop.is_set():
            await _drain(psub, handler)

    async def _publish_forever() -> None:
        """Sample the graph's size on a clock, not on the write counter.

        It was on the write counter for an hour, hanging off the end of the
        checkpoint. That looked economical and broke the one case the gauges
        exist for. Checkpointing is triggered by ``sync_every`` accumulated
        writes, so a space that is read often and written rarely never reached
        it — and **the series was absent rather than stale**, which in Prometheus
        is not a flat line but no line. Graph timeouts happen on reads. The space
        most likely to time out was exactly the one reporting nothing about why.

        A graph that grew large and then went quiet had the same hole from the
        other direction: it stops being sampled precisely when its size stops
        changing and starts being the whole explanation.

        Once immediately, so a process that never writes still reports; then
        every ``GRAPH_SAMPLE_SECONDS``. Four counting queries a minute against a
        SQLite file is nothing next to a single recall, and unlike a scrape it is
        a rate we control.
        """

        await publish_graph_size(kg, memory_space_id=memory_space_id)
        while not stop.is_set():
            await asyncio.sleep(GRAPH_SAMPLE_SECONDS)
            if stop.is_set():
                break
            await publish_graph_size(kg, memory_space_id=memory_space_id)

    async def _checkpoint_forever() -> None:
        """Checkpoint the WAL once enough writes accrue, in its own task.

        Kept off the drain path so checkpointing never blocks message ingestion,
        and a checkpoint failure degrades gracefully (log + retry) instead of
        forcing a NATS reconnect the way it did when it shared the drain loop's
        try-block.

        """
        nonlocal writes_since_checkpoint
        while not stop.is_set():
            await asyncio.sleep(0.5)
            # Snapshot the count BEFORE the awaited checkpoint. Subtracting this
            # snapshot afterwards (rather than resetting to 0) preserves writes
            # the concurrent drain tasks add during the checkpoint window, and —
            # on failure — still consumes the trigger so a persistently failing
            # checkpoint retries at most once per ``sync_every`` writes instead
            # of tight-looping every 0.5s.
            pending = writes_since_checkpoint
            if pending < sync_every:
                continue
            try:

                async def _checkpoint_targets() -> None:
                    # KG is Eidolon-owned SQLite. Chroma's native client owns
                    # chroma.sqlite3 and its compaction lifecycle exclusively.
                    # With the graph off there is no such file to checkpoint.
                    if kg is None:
                        return
                    result = await asyncio.to_thread(
                        checkpoint_sqlite_wal, kg_sqlite, mode="PASSIVE"
                    )
                    await asyncio.to_thread(fsync_directory, Path(kg_sqlite).parent)
                    metrics.GRAPH_WAL_PAGES.set(result.wal_pages)
                    metrics.GRAPH_CHECKPOINT_PAGES.inc(result.checkpointed_pages)
                    if result.stalled:
                        # A log with pages that would not move. Logged and not
                        # raised: the service is still correct, it is just no
                        # longer durable at the rate it thinks it is, and the
                        # cause is usually a reader that will finish on its own.
                        log.warning(
                            "agent_runner_checkpoint_stalled",
                            memory_space_id=memory_space_id,
                            wal_pages=result.wal_pages,
                            busy=result.busy,
                        )

                lock = getattr(backend, "lock", None)
                if lock is not None:
                    async with lock:
                        await _checkpoint_targets()
                else:
                    await _checkpoint_targets()
            except Exception as exc:  # noqa: BLE001 - best-effort durability
                log.warning(
                    "agent_runner_checkpoint_failed",
                    memory_space_id=memory_space_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            # Consume the snapshot on both success and failure (see above).
            writes_since_checkpoint -= pending

    async def _handle_list_drawers_query(msg) -> None:
        """Serve ephemeral consolidation snapshot reads.

        The consolidator is an internal background worker, but reads still go
        through this agent_runner process so Chroma/KG ownership stays single-
        process. This replaces the old MCP HTTP dependency for consolidator
        reads while reusing the same ``LockedBackend`` and privacy filter as
        ``eidolon_memory_list``.
        """
        try:
            payload = json.loads(msg.data.decode("utf-8") or "{}")
            limit = max(1, min(int(payload.get("limit") or 500), 1000))
            offset = max(0, int(payload.get("offset") or 0))
            include_private = bool(payload.get("include_private", False))
            rows = await backend.get_all(memory_space_id, limit=limit, offset=offset)
            filtered = [
                row for row in rows if row_visible_to_listing(row, include_private=include_private)
            ]
            response = {
                "records": [wire_record_to_public_dict(row) for row in filtered],
                "total_hint": len(filtered),
                "limit": limit,
                "offset": offset,
            }
        except Exception as exc:  # noqa: BLE001 - responder returns typed error JSON
            log.warning(
                "agent_runner_query_failed",
                memory_space_id=memory_space_id,
                subject=query_subject,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            response = {
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        await msg.respond(json.dumps(response, ensure_ascii=False).encode("utf-8"))

    # Outer reconnect loop: a dropped / drained NATS connection re-subscribes
    # with exponential backoff instead of exiting. Durable consumers persist
    # server-side, so re-subscribe rebinds and resumes from the last ack.
    reconnect_delay = 1.0
    _MAX_RECONNECT_DELAY = 30.0
    while not stop.is_set():
        nc = None
        try:
            nc = await nats.connect(
                settings.nats.url,
                max_reconnect_attempts=-1,  # infinite connection-level retries
                reconnect_time_wait=2,
            )
            js = nc.jetstream()
            await ensure_memory_stream(js, settings)
            psub_turn = await js.pull_subscribe(
                turn_subject, durable=durable_turn, stream=settings.nats.stream
            )
            psub_cmd = await js.pull_subscribe(
                cmd_subject, durable=durable_cmd, stream=settings.nats.stream
            )
            psub_sync = await js.pull_subscribe(
                sync_subject, durable=durable_sync, stream=settings.nats.stream
            )
            await nc.subscribe(query_subject, cb=_handle_list_drawers_query)
            await nc.flush()
            if ready is not None and not ready.is_set():
                ready.set()
            log.info(
                "agent_runner_nats_pull_subscribe",
                memory_space_id=memory_space_id,
                turn_subject=turn_subject,
                cmd_subject=cmd_subject,
                sync_subject=sync_subject,
                query_subject=query_subject,
                stream=settings.nats.stream,
            )
            reconnect_delay = 1.0  # healthy connection — reset backoff

            # One task per subject (+ checkpointer) so a busy turn subject
            # can't starve the command / sync subjects — see _drain_forever.
            def _turn_handler(m):
                return process_turn_message(
                    m,
                    steward=steward,
                    backend=backend,
                    kg=kg,
                    settings=settings,
                    max_deliveries=settings.nats.worker_max_deliveries,
                    expected_memory_space_id=memory_space_id,
                    # Fanout absorption/rejection is high-frequency operational
                    # telemetry, already represented by Memory metrics and
                    # local ledgers. It must not synchronously write the shared
                    # system-data SQLite database.
                    # No audit observer. The only implementation lived in the
                    # deleted eidolon_data integration and this call site has
                    # always passed None, so nothing changed here — see
                    # ``process_turn_message`` for what an implementation is.
                    audit_sink=None,
                    dlq_writer=dlq,
                    decision_store=decision_store,
                    canonical_facts=canonical_facts,
                )

            def _cmd_handler(m):
                return process_command_message(
                    m,
                    backend=backend,
                    kg=kg,
                    expected_memory_space_id=memory_space_id,
                    settings=settings,
                    command_status=command_status,
                    dlq_writer=dlq,
                    canonical_facts=canonical_facts,
                    commitments=commitments,
                )

            def _sync_handler(m):
                return process_sync_message(
                    m,
                    steward=steward,
                    backend=backend,
                    ledger=ledger,
                    settings=settings,
                    expected_memory_space_id=memory_space_id,
                    decision_store=decision_store,
                    kg=kg,
                )

            workers = [
                asyncio.create_task(_drain_forever(psub_turn, _turn_handler)),
                asyncio.create_task(_drain_forever(psub_cmd, _cmd_handler)),
                asyncio.create_task(_drain_forever(psub_sync, _sync_handler)),
                asyncio.create_task(_checkpoint_forever()),
                asyncio.create_task(_publish_forever()),
            ]
            try:
                # Return as soon as ANY worker dies — a NATS/ack error surfaces
                # here and re-raises into the outer reconnect loop, which rebinds
                # all subscriptions on a fresh connection. A clean return only
                # happens once ``stop`` has drained every worker.
                #
                # Only inspect the FINISHED tasks: FIRST_EXCEPTION returns with
                # the others still pending, and ``.exception()`` on a pending
                # task raises InvalidStateError — which would mask the real error.
                done, _pending = await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        raise task.exception()
            finally:
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001 - any connection/sub failure → reconnect
            if ready is not None and not ready.is_set():
                ready.clear()
            if stop.is_set():
                break
            log.warning(
                "agent_runner_nats_reconnect",
                error=str(exc),
                error_type=type(exc).__name__,
                retry_in_s=reconnect_delay,
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY)
        finally:
            if nc is not None:
                try:
                    await nc.drain()
                except Exception:
                    pass
    log.info("agent_runner_nats_stopped", memory_space_id=memory_space_id)


def _compose_starlette_lifespan(
    mcp: Any,
    ops_mcp: Any = None,
    *,
    memory_space_id: str,
    settings: MemorySettings,
    backend: Any,
    kg: Any,
    command_publisher: Any,
    command_status: CommandStatusLedger,
    dlq: DlqLedger,
    decision_store: ExtractionDecisionLedger,
    canonical_facts: CanonicalFactLedger,
    commitments: CommitmentLedger,
    sync: Any,
    palace_path: str,
):
    """Compose FastMCP's session-manager lifespan with our startup hooks."""
    # Beside the palace, not inside it: MemPalace's repair renames its own
    # directory, so anything of ours kept there is lost on an embedder change.
    kg_sqlite = str(Path(str(palace_path) + LEDGERS_DIR_SUFFIX) / "knowledge_graph.sqlite3")
    stop_event = asyncio.Event()
    nats_ready_event = asyncio.Event()
    session_manager = mcp.session_manager
    ops_session_manager = None if ops_mcp is None else ops_mcp.session_manager
    nats_disabled = os.environ.get("EIDOLON_MEMORY_DISABLE_NATS", "").strip() == "1"

    @asynccontextmanager
    async def _lifespan(_app: Any):
        log.info("agent_runner_warm_start", memory_space_id=memory_space_id, palace=palace_path)
        warm_started = time.perf_counter()
        try:
            await warm_read_path(backend, settings, role="default")
            log.info(
                "agent_runner_warm_complete",
                memory_space_id=memory_space_id,
                elapsed_ms=_elapsed_ms(warm_started),
            )
        except Exception as exc:
            log.warning(
                "agent_runner_warm_failed",
                memory_space_id=memory_space_id,
                error=str(exc),
                elapsed_ms=_elapsed_ms(warm_started),
            )

        sub_task: asyncio.Task | None = None
        if nats_disabled:
            log.warning(
                "agent_runner_nats_disabled",
                memory_space_id=memory_space_id,
                reason="EIDOLON_MEMORY_DISABLE_NATS=1",
            )
        else:
            sub_task = asyncio.create_task(
                _nats_subscriber_loop(
                    memory_space_id=memory_space_id,
                    settings=settings,
                    backend=backend,
                    kg=kg,
                    kg_sqlite=kg_sqlite,
                    sync=sync,
                    command_status=command_status,
                    dlq=dlq,
                    decision_store=decision_store,
                    canonical_facts=canonical_facts,
                    commitments=commitments,
                    stop=stop_event,
                    ready=nats_ready_event,
                ),
                name=f"nats-sub-{memory_space_id}",
            )
        try:
            if not nats_disabled:
                nats_started = time.perf_counter()
                await asyncio.wait_for(nats_ready_event.wait(), timeout=30.0)
                log.info(
                    "agent_runner_nats_ready",
                    memory_space_id=memory_space_id,
                    elapsed_ms=_elapsed_ms(nats_started),
                )
            # Both surfaces' session managers, because both are mounted. A manager
            # that was never started answers every request with a 500, so the
            # operator surface would look mounted and broken rather than absent.
            async with contextlib.AsyncExitStack() as sessions:
                await sessions.enter_async_context(session_manager.run())
                if ops_session_manager is not None:
                    await sessions.enter_async_context(ops_session_manager.run())
                yield
        finally:
            stop_event.set()
            with contextlib.suppress(Exception):
                if sub_task is not None:
                    await asyncio.wait_for(sub_task, timeout=5.0)
            with contextlib.suppress(Exception):
                await command_publisher.close()
            log.info("agent_runner_shutdown_complete", memory_space_id=memory_space_id)

    return _lifespan


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eidolon-memory-agent",
        description="Single-memory-space memory agent runner (D1)",
    )
    parser.add_argument("--memory-space-id", required=True, help="Bound memory-space identifier")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Control-plane MCP port; 0 = use settings.mcp_http.port",
    )
    parser.add_argument(
        "--host",
        default="",
        help="Control-plane bind host; default settings.mcp_http.host (127.0.0.1)",
    )
    parser.add_argument(
        "--palace-path",
        default="",
        help=(
            "Override palace directory "
            "(default $EIDOLON_STATE_ROOT/memory/mempalaces/<memory_space_id>)"
        ),
    )
    # Whose memory this space is. Not a scope check — the space is already the
    # scope — it is what the read path's audience filter compares against.
    # Which Companion is asking is per-request, not per-process: one space
    # serves every Companion this Owner has.
    parser.add_argument("--owner-id", default="", help="Owner this space belongs to")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    bootstrap_started = time.perf_counter()
    args = _parse_args(argv)
    memory_space_id = validate_memory_space_id(args.memory_space_id)
    settings = get_memory_settings()
    apply_mempalace_backend_env(settings)
    backend_name = selected_mempalace_backend(settings)
    apply_cpu_thread_env(settings, role="livekit")

    # Before any store is opened: those stacks read the temp-directory variables
    # while their native extensions load, so this cannot wait until a space is
    # resolved. Process-scoped, because TMPDIR is — see configure_process_temp_root.
    configure_process_temp_root(settings, resolve_palaces_root(settings))

    # Opening a space — its claim, its safety checks, its storage — belongs to the
    # router, so that a process serving several spaces does it once per space
    # rather than once at startup. This process still serves exactly one, which is
    # why the router is restricted to it.
    router = build_space_router(
        settings,
        allowed_spaces=[memory_space_id],
        palace_path_override=args.palace_path or None,
    )
    # Resolving is async and uvicorn has not started yet, so this runs in a
    # throwaway loop. Safe because nothing resolved here binds to it: asyncio
    # locks attach to the loop that first awaits them, which will be uvicorn's.
    step_started = time.perf_counter()
    runtime = asyncio.run(router.resolve(memory_space_id))
    palace_path = Path(runtime.palace_path)
    log.info(
        "agent_runner_space_opened",
        memory_space_id=memory_space_id,
        palace=str(palace_path),
        backend=backend_name,
        kg=runtime.has_kg,
        elapsed_ms=_elapsed_ms(step_started),
    )

    backend = runtime.backend
    kg = runtime.kg
    command_status = runtime.ledgers.command_status
    dlq = runtime.ledgers.dlq
    decision_store = runtime.ledgers.decisions
    canonical_facts = runtime.ledgers.canonical_facts
    commitments = runtime.ledgers.commitments
    # Like every other handle. It used to be built again inside the subscriber
    # loop, which meant two objects on one file — two write locks, so the
    # serialisation that keeps writers out of each other's way did not apply
    # between them. Harmless only because the router's copy had no consumer.
    sync = runtime.ledgers.sync

    # KG plan §3.3: write tools publish through the same JetStream stream that
    # handles chat turns; admin is just another client.
    command_publisher = JetStreamCommandPublisher.from_memory_settings(settings)

    _run_service(
        args=args,
        settings=settings,
        memory_space_id=memory_space_id,
        backend_name=backend_name,
        palace_path=palace_path,
        router=router,
        backend=backend,
        kg=kg,
        command_publisher=command_publisher,
        command_status=command_status,
        dlq=dlq,
        decision_store=decision_store,
        canonical_facts=canonical_facts,
        commitments=commitments,
        sync=sync,
        bootstrap_started=bootstrap_started,
    )


def _run_service(
    *,
    args: Any,
    settings: MemorySettings,
    memory_space_id: str,
    backend_name: str,
    palace_path: Path,
    router: Any,
    backend: Any,
    kg: Any,
    command_publisher: Any,
    command_status: Any,
    dlq: Any,
    decision_store: Any,
    canonical_facts: Any,
    commitments: Any,
    sync: Any,
    bootstrap_started: float,
) -> None:
    """Bind the MCP surface and serve until shut down."""

    host = (args.host or settings.mcp_http.host).strip() or "127.0.0.1"
    port = args.port if args.port else settings.mcp_http.port

    step_started = time.perf_counter()
    # Both halves of the boundary go through the service, which resolves a space
    # from the caller's context rather than from this process. That is what lets
    # one process serve several spaces; how many it actually holds is the router's
    # decision.
    #
    # ``command_status`` is handed over so an explicit write can wait for a real
    # outcome instead of guessing one — without it every such write answers
    # ``accepted``, which is honest but never lets a caller say "remembered".
    #
    # No turn publisher: this process *consumes* turns off the bus, it does not
    # produce them. ``publish_turn`` therefore answers ``skipped_no_bus`` here,
    # which is the truthful answer for a runner rather than a missing feature.
    service = MemoryService(
        router,
        settings,
        command_publisher=command_publisher,
        command_status=command_status,
    )

    # Two surfaces, one port, one set of handles. The agent's keeps the path it
    # always had, so neither discovery nor the agent repo changes; the operator
    # surface moves to its own. Both are built from the same arguments — the split
    # is who may see which tool, not which store they reach.
    def _surface(surface: str, path: str | None):
        return build_control_plane_mcp(
            backend,
            settings,
            service=service,
            memory_space_id=memory_space_id,
            palace_path=str(palace_path),
            host=host,
            port=port,
            lifespan=None,
            kg=kg,
            command_publisher=command_publisher,
            command_status=command_status,
            canonical_facts=canonical_facts,
            commitments=commitments,
            dlq_store=dlq,
            replay_publisher=command_publisher,
            surface=surface,
            path=path,
        )

    mcp = _surface("agent", None)
    ops_mcp = _surface("all", settings.mcp_http.ops_path)
    log.info(
        "agent_runner_mcp_build_done",
        memory_space_id=memory_space_id,
        agent_tools=len(mcp._tool_manager.list_tools()),
        ops_tools=len(ops_mcp._tool_manager.list_tools()),
        agent_path=settings.mcp_http.path,
        ops_path=settings.mcp_http.ops_path,
        elapsed_ms=_elapsed_ms(step_started),
    )

    log.info(
        "agent_runner_start",
        memory_space_id=memory_space_id,
        palace=str(palace_path),
        backend=backend_name,
        host=host,
        port=port,
        bootstrap_elapsed_ms=_elapsed_ms(bootstrap_started),
    )

    def _on_signal(signum: int, _frame: Any) -> None:  # pragma: no cover - signal path
        log.info(
            "agent_runner_signal_received",
            memory_space_id=memory_space_id,
            signum=signum,
        )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    # Bypass mcp.run(): build the Starlette ASGI app, replace its lifespan with
    # our composite (warm + NATS sub → session_manager.run()), then drive
    # uvicorn ourselves.
    import uvicorn

    starlette_app = mcp.streamable_http_app()
    _mount_metrics(starlette_app)
    # Before the ops surface, which is mounted at "" and would otherwise answer
    # for every path beneath it.
    starlette_app.router.routes.append(
        recollections_route(
            service=service,
            settings=settings,
            memory_space_id=memory_space_id,
            owner_id=args.owner_id or None,
            # Resolved here rather than inside the route, so a Host with no
            # credential provisioned fails closed at the boundary instead of
            # somewhere deeper with a less useful message.
            service_token=settings.mcp_http.resolve_api_service_token(),
        )
    )
    _mount_ops_surface(starlette_app, ops_mcp, path=settings.mcp_http.ops_path)
    starlette_app.router.lifespan_context = _compose_starlette_lifespan(
        mcp,
        ops_mcp,
        memory_space_id=memory_space_id,
        settings=settings,
        backend=backend,
        kg=kg,
        command_publisher=command_publisher,
        command_status=command_status,
        dlq=dlq,
        decision_store=decision_store,
        canonical_facts=canonical_facts,
        commitments=commitments,
        sync=sync,
        palace_path=str(palace_path),
    )

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        asyncio.run(server.serve())
    finally:
        # The router holds this process's claim on every space it opened, so
        # releasing is its business now rather than a lone file handle's.
        with contextlib.suppress(Exception):
            asyncio.run(router.aclose())


if __name__ == "__main__":
    main()
