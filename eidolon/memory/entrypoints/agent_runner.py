"""Single-memory-space agent runner (D1).

One ``eidolon-memory-agent --memory-space-id=<id> --port=<P>`` process per memory space:

* owns the only ``chromadb.PersistentClient`` pointing at the memory-space palace
* wraps that backend in ``LockedBackend`` so reads + writes share one
  ``asyncio.Lock``
* hosts the FastMCP control-plane on the loopback port (Admin / Claude IDE)
* runs the NATS JetStream subscriber for ``eidolon.memory.turn.<memory_space_token>``
* runs the steward in-process (writes never leave this process)

LiveKit pipelines that live in the same process call the recall path directly
via ``LiveKitRecallService``; they share the same lock through the same backend.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from eidolon_sdk.memory import (
    conversation_turn_subject,
    memory_command_subject,
    memory_sync_subject,
)

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.eidolon_data_runtime import (
    EidolonDataMemoryFanoutAuditSink,
)
from eidolon.memory.application.privacy_filter import row_visible_to_listing
from eidolon.memory.application.public_recall import wire_record_to_public_dict
from eidolon.memory.application.runtime_warm import warm_palace_read_path
from eidolon.memory.application.steward import create_steward
from eidolon.memory.application.turn_processor import (
    process_command_message,
    process_sync_message,
    process_turn_message,
)
from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon.memory.config.memory_settings import (
    MemorySettings,
    get_memory_settings,
    resolve_run_dir,
)
from eidolon.memory.config.palace_directory import (
    resolve_palace_for_memory_space,
    validate_memory_space_id,
)
from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp
from eidolon.memory.infrastructure.chroma_refresh import checkpoint_sqlite_wal
from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env
from eidolon.memory.infrastructure.integrity import (
    IntegrityCheckFailed,
    PalaceLocationError,
    assert_palace_location_safe,
    fsync_directory,
    run_integrity_check,
)
from eidolon.memory.infrastructure.mempalace_backend import (
    apply_mempalace_backend_env,
    mempalace_backend_env,
    selected_mempalace_backend,
    vector_sqlite_integrity_targets,
)
from eidolon.memory.infrastructure.nats.commands import JetStreamCommandPublisher
from eidolon.memory.infrastructure.nats.names import memory_consumer_name, nats_safe_name
from eidolon.memory.infrastructure.nats.query import memory_list_drawers_query_subject
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized
from eidolon.memory.infrastructure.sync_ledger import SyncLedger
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


def _acquire_memory_space_process_lock(settings: MemorySettings, memory_space_id: str):
    """Hold an exclusive process lock for one memory space.

    This protects the JetStream durable-consumer ownership contract as well as
    the palace single-owner rule. Starting two agent_runners for the same
    memory_space_id can route writes to the wrong palace if one is a temporary
    benchmark process, so fail fast instead.
    """
    run_dir = resolve_run_dir(settings)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / f"eidolon-memory-agent-{nats_safe_name(memory_space_id)}.lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.seek(0)
        holder = handle.read().strip()
        handle.close()
        msg = (
            f"memory_space_id {memory_space_id!r} is already owned by another "
            f"eidolon-memory-agent; lock={lock_path} holder={holder!r}"
        )
        raise RuntimeError(msg) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _materialize_kg_file(kg_sqlite_path: Path) -> None:
    """Ensure ``knowledge_graph.sqlite3`` exists with the schema committed.

    mempalace.KnowledgeGraph() creates the file + tables on first ``__init__``;
    we explicitly open + close so the file is present *before* integrity_check
    runs. fsync the parent directory so the new file survives a sudden poweroff.
    """
    from mempalace.knowledge_graph import KnowledgeGraph

    kg_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    kg = KnowledgeGraph(db_path=str(kg_sqlite_path))
    try:
        kg.close()
    except Exception as exc:
        log.warning("kg_materialize_close_failed", error=str(exc))
    fsync_directory(kg_sqlite_path.parent)


def _open_fanout_audit_sink() -> Any:
    """Best-effort audit sink for agent→memory fanout closure.

    Opens an events-only DataStore against the shared eidolon_data DB. Returns
    None if unavailable — the turn path then behaves exactly as before.
    """
    try:
        from eidolon_data import DataSettings, DataStore

        return EidolonDataMemoryFanoutAuditSink(DataStore.open(DataSettings()))
    except Exception as exc:  # noqa: BLE001 - audit is optional, never fatal
        log.warning("fanout_audit_sink_unavailable", error=str(exc))
        return None


async def _nats_subscriber_loop(
    *,
    memory_space_id: str,
    settings: MemorySettings,
    backend: Any,
    kg: Any,
    palace_sqlite: str | None,
    kg_sqlite: str,
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
    ledger = SyncLedger(Path(kg_sqlite).parent / "sync_ledger.sqlite3")

    steward = create_steward(settings)
    audit_sink = _open_fanout_audit_sink()
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
                    # G4: local SQLite backends are WAL — checkpoint when present.
                    if palace_sqlite:
                        await asyncio.to_thread(
                            checkpoint_sqlite_wal, palace_sqlite, mode="PASSIVE"
                        )
                    await asyncio.to_thread(checkpoint_sqlite_wal, kg_sqlite, mode="PASSIVE")
                    await asyncio.to_thread(fsync_directory, Path(kg_sqlite).parent)

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
                    audit_sink=audit_sink,
                )

            def _cmd_handler(m):
                return process_command_message(
                    m,
                    backend=backend,
                    kg=kg,
                    expected_memory_space_id=memory_space_id,
                    settings=settings,
                )

            def _sync_handler(m):
                return process_sync_message(
                    m,
                    steward=steward,
                    backend=backend,
                    ledger=ledger,
                    settings=settings,
                    expected_memory_space_id=memory_space_id,
                )

            workers = [
                asyncio.create_task(_drain_forever(psub_turn, _turn_handler)),
                asyncio.create_task(_drain_forever(psub_cmd, _cmd_handler)),
                asyncio.create_task(_drain_forever(psub_sync, _sync_handler)),
                asyncio.create_task(_checkpoint_forever()),
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
                done, _pending = await asyncio.wait(
                    workers, return_when=asyncio.FIRST_EXCEPTION
                )
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
    *,
    memory_space_id: str,
    settings: MemorySettings,
    backend: Any,
    kg: Any,
    command_publisher: Any,
    palace_path: str,
):
    """Compose FastMCP's session-manager lifespan with our startup hooks."""
    backend_name = selected_mempalace_backend(settings)
    palace_sqlite = str(Path(palace_path) / "chroma.sqlite3") if backend_name == "chroma" else None
    kg_sqlite = str(Path(palace_path) / "knowledge_graph.sqlite3")
    stop_event = asyncio.Event()
    nats_ready_event = asyncio.Event()
    session_manager = mcp.session_manager
    nats_disabled = os.environ.get("EIDOLON_MEMORY_DISABLE_NATS", "").strip() == "1"

    @asynccontextmanager
    async def _lifespan(_app: Any):
        log.info("agent_runner_warm_start", memory_space_id=memory_space_id, palace=palace_path)
        warm_started = time.perf_counter()
        try:
            await warm_palace_read_path(settings, palace_path, role="default")
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
                    palace_sqlite=palace_sqlite,
                    kg_sqlite=kg_sqlite,
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
            async with session_manager.run():
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
        help="Override palace directory (default ~/eidolon/memory/mempalaces/<memory_space_id>)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    bootstrap_started = time.perf_counter()
    args = _parse_args(argv)
    memory_space_id = validate_memory_space_id(args.memory_space_id)
    settings = get_memory_settings()
    process_lock = _acquire_memory_space_process_lock(settings, memory_space_id)
    apply_mempalace_backend_env(settings)
    backend_name = selected_mempalace_backend(settings)
    apply_cpu_thread_env(settings, role="livekit")

    palace_path = resolve_palace_for_memory_space(
        settings,
        memory_space_id,
        path_override=args.palace_path or None,
    )

    # D4: deployment-location guard (iCloud / Dropbox / NFS / SMB)
    try:
        assert_palace_location_safe(palace_path)
    except PalaceLocationError as exc:
        log.error("agent_runner_unsafe_palace_location", error=str(exc))
        raise

    step_started = time.perf_counter()
    ensure_palace_initialized(
        memory_space_id,
        palace_path,
        backend=backend_name,
        env=mempalace_backend_env(settings),
    )
    log.info(
        "agent_runner_palace_init_done",
        memory_space_id=memory_space_id,
        palace=str(palace_path),
        elapsed_ms=_elapsed_ms(step_started),
    )

    # KG plan §3.2 G3: explicitly create the KG SQLite so integrity_check sees a
    # committed file (mempalace KnowledgeGraph initializes tables on first open).
    kg_sqlite_path = palace_path / "knowledge_graph.sqlite3"
    step_started = time.perf_counter()
    _materialize_kg_file(kg_sqlite_path)
    log.info(
        "agent_runner_kg_materialize_done",
        memory_space_id=memory_space_id,
        kg=str(kg_sqlite_path),
        elapsed_ms=_elapsed_ms(step_started),
    )

    # D2 + KG G3: integrity check — refuse to come up on a malformed palace OR KG.
    integrity_targets = [
        *vector_sqlite_integrity_targets(palace_path, backend_name),
        ("kg", kg_sqlite_path),
    ]
    step_started = time.perf_counter()
    checked: list[str] = []
    for label, db_path in integrity_targets:
        report = run_integrity_check(str(db_path), quick=False)
        if not report.ok:
            msg = (
                f"agent_runner refusing to start: {label} integrity_check failed "
                f"({report.detail!r}); investigate and restore from snapshot"
            )
            log.error(
                "agent_runner_integrity_failed",
                memory_space_id=memory_space_id,
                db=label,
                palace=str(palace_path),
                detail=report.detail,
            )
            raise IntegrityCheckFailed(msg)
        checked.append(label)
    log.info(
        "agent_runner_integrity_check_done",
        memory_space_id=memory_space_id,
        targets=checked,
        elapsed_ms=_elapsed_ms(step_started),
    )

    step_started = time.perf_counter()
    inner = MemPalacePythonBackend(settings, str(palace_path), memory_space_id=memory_space_id)
    backend = LockedBackend(inner)

    # Phase 2: bolt the working-memory ring onto the backend so it shares
    # ``backend.lock`` (no second lock to reason about, no deadlock risk).
    # ``maxlen=0`` from settings disables it cleanly — rollback is config-only.
    backend.working_memory = WorkingMemoryRing(
        maxlen=settings.runtime.working_memory_maxlen,
        lock=backend.lock,
    )

    # KG plan §3.0: LockedKnowledgeGraph shares backend.lock so chroma + KG
    # reads/writes stay coherent inside one agent_runner process.
    from mempalace.knowledge_graph import KnowledgeGraph

    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(kg_sqlite_path)),
        backend.lock,
    )

    # KG plan §3.3: write tools publish through the same JetStream stream
    # that handles chat turns; admin is just another "agent" client.
    command_publisher = JetStreamCommandPublisher.from_memory_settings(settings)
    log.info(
        "agent_runner_backend_open_done",
        memory_space_id=memory_space_id,
        elapsed_ms=_elapsed_ms(step_started),
    )

    host = (args.host or settings.mcp_http.host).strip() or "127.0.0.1"
    port = args.port if args.port else settings.mcp_http.port

    step_started = time.perf_counter()
    mcp = build_control_plane_mcp(
        backend,
        settings,
        memory_space_id=memory_space_id,
        palace_path=str(palace_path),
        host=host,
        port=port,
        lifespan=None,
        kg=kg,
        command_publisher=command_publisher,
    )
    log.info(
        "agent_runner_mcp_build_done",
        memory_space_id=memory_space_id,
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
    starlette_app.router.lifespan_context = _compose_starlette_lifespan(
        mcp,
        memory_space_id=memory_space_id,
        settings=settings,
        backend=backend,
        kg=kg,
        command_publisher=command_publisher,
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
        with contextlib.suppress(Exception):
            fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)
        process_lock.close()


if __name__ == "__main__":
    main()
