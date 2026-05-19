"""Single-user agent runner (D1).

One ``eidolon-memory-agent --user-id=<id> --port=<P>`` process per user:

* owns the only ``chromadb.PersistentClient`` pointing at the user's palace
* wraps that backend in ``LockedBackend`` so reads + writes share one
  ``asyncio.Lock``
* hosts the FastMCP control-plane on the loopback port (Admin / Claude IDE)
* runs the NATS JetStream subscriber for ``agent.memory.conversation.turn.<id>``
* runs the steward in-process (writes never leave this process)

LiveKit pipelines that live in the same process call the recall path directly
via ``LiveKitRecallService``; they share the same lock through the same backend.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from contextlib import asynccontextmanager
from typing import Any

from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.runtime_warm import warm_palace_read_path
from eidolon.memory.application.steward import create_steward
from eidolon.memory.application.turn_processor import process_turn_message
from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.palace_directory import (
    resolve_palace_for_user,
    validate_user_id,
)
from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp
from eidolon.memory.infrastructure.bus.subjects import conversation_turn_subject
from eidolon.memory.infrastructure.chroma_refresh import checkpoint_sqlite_wal
from eidolon.memory.infrastructure.integrity import (
    IntegrityCheckFailed,
    PalaceLocationError,
    assert_palace_location_safe,
    fsync_directory,
    run_integrity_check,
)
from eidolon.memory.infrastructure.nats_stream import ensure_memory_stream
from eidolon.memory.infrastructure.palace_init import ensure_palace_initialized
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


async def _nats_subscriber_loop(
    *,
    user_id: str,
    settings: MemorySettings,
    backend: Any,
    palace_sqlite: str,
    stop: asyncio.Event,
) -> None:
    """In-process JetStream pull-subscriber bound to one user_id."""
    import nats

    durable = f"{settings.nats.durable_prefix}-{user_id}"
    subject = conversation_turn_subject(user_id)

    nc = await nats.connect(settings.nats.url)
    js = nc.jetstream()
    try:
        await ensure_memory_stream(js, settings)
        psub = await js.pull_subscribe(subject, durable=durable, stream=settings.nats.stream)
        log.info(
            "agent_runner_nats_pull_subscribe",
            user_id=user_id,
            subject=subject,
            durable=durable,
            stream=settings.nats.stream,
        )
        steward = create_steward(settings)
        sync_every = max(1, settings.worker.sync_every_n_turns)
        turns_since_checkpoint = 0
        while not stop.is_set():
            try:
                msgs = await psub.fetch(8, timeout=2.0)
            except TimeoutError:
                continue
            except Exception as exc:
                log.warning("agent_runner_nats_fetch_error", error=str(exc))
                await asyncio.sleep(1.0)
                continue
            for msg in msgs:
                await process_turn_message(
                    msg,
                    steward=steward,
                    backend=backend,
                    settings=settings,
                    max_deliveries=settings.nats.worker_max_deliveries,
                    expected_user_id=user_id,
                )
                turns_since_checkpoint += 1
                if turns_since_checkpoint >= sync_every:
                    await asyncio.to_thread(
                        checkpoint_sqlite_wal, palace_sqlite, mode="PASSIVE"
                    )
                    await asyncio.to_thread(
                        fsync_directory, Path(palace_sqlite).parent
                    )
                    turns_since_checkpoint = 0
    finally:
        try:
            await nc.drain()
        except Exception:
            pass
        log.info("agent_runner_nats_stopped", user_id=user_id)


def _compose_starlette_lifespan(
    mcp: Any,
    *,
    user_id: str,
    settings: MemorySettings,
    backend: Any,
    palace_path: str,
):
    """Compose FastMCP's session-manager lifespan with our startup hooks.

    FastMCP wires the user-supplied ``lifespan`` to the MCP protocol session
    (per-connection); the *Starlette* ASGI lifespan it installs is hard-coded
    to ``session_manager.run()``. So we replace Starlette's lifespan with a
    composite that runs our warm + NATS subscriber FIRST, then enters
    ``session_manager.run()``.
    """
    from pathlib import Path

    palace_sqlite = str(Path(palace_path) / "chroma.sqlite3")
    stop_event = asyncio.Event()
    session_manager = mcp.session_manager

    @asynccontextmanager
    async def _lifespan(_app: Any):
        log.info("agent_runner_warm_start", user_id=user_id, palace=palace_path)
        try:
            await warm_palace_read_path(settings, palace_path, role="default")
            log.info("agent_runner_warm_complete", user_id=user_id)
        except Exception as exc:
            log.warning("agent_runner_warm_failed", user_id=user_id, error=str(exc))

        sub_task = asyncio.create_task(
            _nats_subscriber_loop(
                user_id=user_id,
                settings=settings,
                backend=backend,
                palace_sqlite=palace_sqlite,
                stop=stop_event,
            ),
            name=f"nats-sub-{user_id}",
        )
        try:
            async with session_manager.run():
                yield
        finally:
            stop_event.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(sub_task, timeout=5.0)
            log.info("agent_runner_shutdown_complete", user_id=user_id)

    return _lifespan


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eidolon-memory-agent",
        description="Single-user memory agent runner (D1)",
    )
    parser.add_argument("--user-id", required=True, help="Bound user identifier")
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
        help="Override palace directory (default ~/eidolon/palaces/<user_id>)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env

    args = _parse_args(argv)
    user_id = validate_user_id(args.user_id)
    settings = get_memory_settings()
    apply_cpu_thread_env(settings, role="livekit")

    palace_path = (
        resolve_palace_for_user(
            settings,
            user_id,
            path_override=args.palace_path or None,
        )
    )

    # D4: deployment-location guard (iCloud / Dropbox / NFS / SMB)
    try:
        assert_palace_location_safe(palace_path)
    except PalaceLocationError as exc:
        log.error("agent_runner_unsafe_palace_location", error=str(exc))
        raise

    ensure_palace_initialized(user_id, palace_path)

    # D2: integrity check — refuse to come up on a malformed palace
    sqlite_path = palace_path / "chroma.sqlite3"
    report = run_integrity_check(str(sqlite_path), quick=False)
    if not report.ok:
        msg = (
            f"agent_runner refusing to start: palace integrity_check failed "
            f"({report.detail!r}); investigate and restore from snapshot"
        )
        log.error(
            "agent_runner_integrity_failed",
            user_id=user_id,
            palace=str(palace_path),
            detail=report.detail,
        )
        raise IntegrityCheckFailed(msg)

    inner = MemPalacePythonBackend(settings, str(palace_path))
    backend = LockedBackend(inner)

    host = (args.host or settings.mcp_http.host).strip() or "127.0.0.1"
    port = args.port if args.port else settings.mcp_http.port

    mcp = build_control_plane_mcp(
        backend,
        settings,
        user_id=user_id,
        palace_path=str(palace_path),
        host=host,
        port=port,
        lifespan=None,
    )

    log.info(
        "agent_runner_start",
        user_id=user_id,
        palace=str(palace_path),
        host=host,
        port=port,
    )

    def _on_signal(signum: int, _frame: Any) -> None:  # pragma: no cover - signal path
        log.info("agent_runner_signal_received", user_id=user_id, signum=signum)

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
        user_id=user_id,
        settings=settings,
        backend=backend,
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
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
