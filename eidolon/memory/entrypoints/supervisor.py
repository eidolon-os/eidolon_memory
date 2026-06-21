"""Multi-user agent_runner supervisor (D1).

Reads ``users.yaml``, eager-inits each enabled user's palace via the
``ensure_palace_initialized`` helper (subprocess — never touches chromadb in
this process), then spawns one ``eidolon-memory-agent --user-id=<id> --port=<P>``
subprocess per user.

Monitors children; restarts with exponential backoff on crash; degrades a user
after repeated failures. Handles ``SIGHUP`` to re-read ``users.yaml`` and reconcile
the running set (spawn new, terminate disabled / removed, restart on port change).

Critical: this process **must never open chromadb** — forking that state into
agent_runner subprocesses would share SQLite file descriptors and corrupt the
palace. Use only file I/O, subprocess control, and signals here.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eidolon.memory.application.user_admin import UserAdmin
from eidolon.memory.config.memory_settings import (
    MemorySettings,
    get_memory_settings,
    resolve_log_dir,
)
from eidolon.memory.config.palace_directory import resolve_palace_for_user
from eidolon.memory.config.users import (
    UserEntry,
    UsersConfig,
    load_users_config,
    resolve_users_file_path,
)
from eidolon.memory.entrypoints.admin_api import build_admin_api
from eidolon.memory.infrastructure.mempalace_backend import (
    mempalace_backend_env,
    selected_mempalace_backend,
)
from eidolon.memory.infrastructure.palace_init import (
    PalaceInitError,
    ensure_palace_initialized,
    _resolve_mempalace_cli,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_AGENT_CLI = "eidolon-memory-agent"
_CONSOLIDATOR_CLI = "eidolon-memory-consolidator"
_DEGRADED_MIN_INTERVAL = 60.0  # seconds — rolling failure window


def _agent_cli_argv(user: UserEntry, palace_path: Path) -> list[str]:
    argv = [_AGENT_CLI, "--user-id", user.id, "--port", str(user.port)]
    if user.palace_path:
        argv += ["--palace-path", str(palace_path)]
    return argv


def _consolidator_cli_argv(user: UserEntry) -> list[str]:
    """Phase 4 — build the consolidator argv for a user whose
    ``consolidator.enabled = True``. Assumes the caller already checked
    ``user.consolidator_enabled()``.
    """
    cfg = user.consolidator
    assert cfg is not None and cfg.enabled, (
        "consolidator argv asked for a user without enabled config"
    )
    return [
        _CONSOLIDATOR_CLI,
        "--user-id", user.id,
        "--mcp-url", f"http://127.0.0.1:{user.port}/mcp",
        "--interval-hours", str(cfg.interval_hours),
        "--window-days", str(cfg.window_days),
        "--min-drawers", str(cfg.min_drawers),
        "--min-confidence", str(cfg.min_confidence),
    ]


def _open_child_log(
    log_root: Path, user_id: str, *, prefix: str = "agent"
) -> tuple[Path, subprocess._FILE]:
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / f"{prefix}_{user_id}.log"
    fh = log_path.open("ab", buffering=0)
    return log_path, fh


class _Child:
    """One supervised subprocess (agent_runner OR consolidator).

    ``kind`` discriminates the two roles for logs / log-file naming /
    diagnostics; the lifecycle (spawn, monitor, backoff, terminate) is
    identical so we don't subclass.
    """

    def __init__(
        self,
        user: UserEntry,
        palace_path: Path,
        log_root: Path,
        *,
        kind: str = "agent",
    ) -> None:
        self.user = user
        self.palace_path = palace_path
        self.log_root = log_root
        self.kind = kind
        self.proc: subprocess.Popen | None = None
        self.log_path: Path | None = None
        self._log_fh = None
        self.failure_times: deque[float] = deque()
        self.backoff_idx: int = 0
        self.degraded: bool = False
        self.last_spawn_at: float = 0.0

    def _build_argv(self) -> list[str]:
        if self.kind == "consolidator":
            return _consolidator_cli_argv(self.user)
        return _agent_cli_argv(self.user, self.palace_path)

    def spawn(self) -> None:
        self.log_path, self._log_fh = _open_child_log(
            self.log_root, self.user.id, prefix=self.kind,
        )
        argv = self._build_argv()
        log.info(
            "supervisor_spawn",
            user_id=self.user.id,
            kind=self.kind,
            port=self.user.port,
            argv=argv,
            log_path=str(self.log_path),
        )
        self.proc = subprocess.Popen(
            argv,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.last_spawn_at = time.monotonic()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def terminate(self, *, grace_seconds: float = 30.0) -> None:
        if self.proc is None:
            return
        log.info(
            "supervisor_terminate",
            user_id=self.user.id, kind=self.kind, pid=self.proc.pid,
        )
        try:
            self.proc.terminate()
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            log.warning(
                "supervisor_kill",
                user_id=self.user.id, kind=self.kind, pid=self.proc.pid,
            )
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        self._close_log()

    def _close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def record_failure(self, max_failures_per_minute: int) -> None:
        now = time.monotonic()
        self.failure_times.append(now)
        while self.failure_times and now - self.failure_times[0] > _DEGRADED_MIN_INTERVAL:
            self.failure_times.popleft()
        if len(self.failure_times) >= max_failures_per_minute:
            self.degraded = True
            log.error(
                "supervisor_user_degraded",
                user_id=self.user.id, kind=self.kind,
                failures_in_window=len(self.failure_times),
            )

    def next_backoff(self, schedule: list[int]) -> int:
        if not schedule:
            return 5
        idx = min(self.backoff_idx, len(schedule) - 1)
        delay = schedule[idx]
        self.backoff_idx += 1
        return delay


class Supervisor:
    def __init__(
        self,
        settings: MemorySettings,
        users_path: Path | None = None,
        *,
        eager_init: bool | None = None,
    ) -> None:
        self._settings = settings
        self._users_path = users_path
        self._log_root = resolve_log_dir(settings)
        self._eager_init = (
            settings.supervisor.eager_init if eager_init is None else eager_init
        )
        self._children: dict[str, _Child] = {}            # agent_runner children
        self._consolidators: dict[str, _Child] = {}       # Phase 4: per-user theme worker
        self._reload_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._init_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="supervisor-init"
        )

    # -------------------- public surface for the admin HTTP layer --------------------
    #
    # ``user_admin.UserAdmin`` drives the supervisor through these. Kept thin
    # and side-effect-free at the read end; the only writer is reconcile_now.

    @property
    def users_path(self) -> Path | None:
        return self._users_path

    def is_worker_alive(self, user_id: str) -> bool:
        child = self._children.get(user_id)
        return child is not None and child.is_alive()

    def palace_path_for(self, user: UserEntry) -> Path:
        return self._palace_for(user)

    async def reconcile_now(self) -> None:
        """Run one reconcile pass synchronously (await until children align).

        The supervisor's main run loop also reconciles on ``request_reload``,
        but admin HTTP handlers cannot just set the event and return — they
        need to await the alignment to know whether the spawn/terminate
        succeeded. This is the same body as the loop's internal call.
        """
        await self._reconcile()

    async def rebuild_memory_index(self, user: UserEntry, *, log_path: Path) -> dict:
        """Rebuild one user's MemPalace vector index without opening Chroma here.

        The supervisor owns child lifecycles, so maintenance runs here:
        stop the user's agent/consolidator, execute the MemPalace CLI in a
        subprocess, then reconcile back to the admin registry.
        """
        user_id = user.id
        palace_path = self._palace_for(user)
        backend = selected_mempalace_backend(self._settings)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(self._terminate_consolidator, user_id)
        child = self._children.pop(user_id, None)
        if child is not None:
            await asyncio.to_thread(child.terminate, grace_seconds=30.0)

        cli = _resolve_mempalace_cli()
        cmd = [
            cli,
            "--backend",
            backend,
            "--palace",
            str(palace_path),
            "repair",
            "--mode",
            "from-sqlite",
            "--archive-existing",
            "--yes",
        ]
        log.info(
            "supervisor_rebuild_index_start",
            user_id=user_id,
            palace=str(palace_path),
            backend=backend,
            log_path=str(log_path),
        )
        with log_path.open("ab", buffering=0) as fh:
            fh.write(
                (
                    f"\n[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
                    f"running: {' '.join(cmd)}\n"
                ).encode("utf-8")
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=mempalace_backend_env(self._settings),
            )
            returncode = await proc.wait()
            fh.write(
                (
                    f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
                    f"exit: {returncode}\n"
                ).encode("utf-8")
            )

        await self._reconcile()
        log.info(
            "supervisor_rebuild_index_done",
            user_id=user_id,
            palace=str(palace_path),
            backend=backend,
            returncode=returncode,
        )
        return {
            "user_id": user_id,
            "palace_path": str(palace_path),
            "backend": backend,
            "returncode": returncode,
            "log_path": str(log_path),
        }

    # -------------------- config loading --------------------

    def _read_users(self) -> UsersConfig:
        return load_users_config(self._settings, path=self._users_path)

    def _palace_for(self, user: UserEntry) -> Path:
        return resolve_palace_for_user(
            self._settings,
            user.id,
            path_override=user.palace_path or None,
        )

    async def _init_users_parallel(self, users: list[UserEntry]) -> set[str]:
        """Eager init in a 4-wide thread pool. Returns the set of user_ids that succeeded."""
        if not self._eager_init or not users:
            return {u.id for u in users}

        tasks = [self._init_user(u) for u in users]
        results = await asyncio.gather(*tasks)
        ok: set[str] = set()
        for user_id, err in results:
            if err is None:
                ok.add(user_id)
            else:
                log.error("supervisor_palace_init_failed", user_id=user_id, error=err)
        return ok

    async def _init_user(self, user: UserEntry) -> tuple[str, str | None]:
        if not self._eager_init:
            return user.id, None

        loop = asyncio.get_running_loop()

        def _run() -> tuple[str, str | None]:
            try:
                ensure_palace_initialized(
                    user.id,
                    self._palace_for(user),
                    backend=selected_mempalace_backend(self._settings),
                    env=mempalace_backend_env(self._settings),
                )
                return user.id, None
            except PalaceInitError as exc:
                return user.id, str(exc)

        return await loop.run_in_executor(self._init_pool, _run)

    # -------------------- lifecycle --------------------

    async def start(self) -> None:
        users = self._read_users()
        enabled = users.enabled_users()
        log.info(
            "supervisor_start",
            users_path=str(self._users_path),
            enabled=len(enabled),
            eager_init=self._eager_init,
        )
        by_id = {u.id: u for u in enabled}
        init_tasks = [asyncio.create_task(self._init_user(u)) for u in enabled]
        for task in asyncio.as_completed(init_tasks):
            user_id, err = await task
            if err is not None:
                log.error("supervisor_palace_init_failed", user_id=user_id, error=err)
                continue
            user = by_id[user_id]
            child = _Child(user, self._palace_for(user), self._log_root)
            child.spawn()
            self._children[user.id] = child
            # Phase 4 — opt-in per-user consolidator. Spawn it alongside the
            # agent_runner; it's a separate process so an LLM blip or
            # consolidator crash leaves the chat path untouched.
            if user.consolidator_enabled():
                self._spawn_consolidator(user)

    def _spawn_consolidator(self, user: UserEntry) -> None:
        """Create + start the consolidator child for ``user``. No-op if one
        is already alive — call ``terminate`` first if you need to restart.
        """
        existing = self._consolidators.get(user.id)
        if existing is not None and existing.is_alive():
            return
        child = _Child(
            user, self._palace_for(user), self._log_root, kind="consolidator",
        )
        try:
            child.spawn()
            self._consolidators[user.id] = child
        except Exception as exc:  # noqa: BLE001 - never block agent spawn
            log.error(
                "supervisor_consolidator_spawn_failed",
                user_id=user.id, error=str(exc),
            )

    async def stop(self) -> None:
        log.info(
            "supervisor_stop_begin",
            agents=len(self._children),
            consolidators=len(self._consolidators),
        )
        # Stop consolidators first — they're read-side, can be killed cheaply
        # without losing state. agent_runner gets the full 30s grace so its
        # NATS draining + WAL checkpoint finishes cleanly.
        for child in list(self._consolidators.values()):
            child.terminate(grace_seconds=10.0)
        self._consolidators.clear()
        for child in list(self._children.values()):
            child.terminate(grace_seconds=30.0)
        self._children.clear()
        self._init_pool.shutdown(wait=False, cancel_futures=True)
        log.info("supervisor_stop_done")

    # -------------------- monitor loop --------------------

    async def run(self) -> None:
        await self.start()
        try:
            while not self._stop_event.is_set():
                if self._reload_event.is_set():
                    self._reload_event.clear()
                    await self._reconcile()
                self._check_children()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                except TimeoutError:
                    pass
        finally:
            await self.stop()

    def _check_children(self) -> None:
        """Single pass over both agent + consolidator children; same logic."""
        backoff = self._settings.supervisor.restart_backoff_seconds
        max_fail = self._settings.supervisor.max_failures_per_minute
        # Iterate agents and consolidators with one loop — both are ``_Child``
        # instances with identical lifecycle semantics, only the spawn argv
        # differs.
        for tracked in (self._children, self._consolidators):
            for child in list(tracked.values()):
                if child.degraded:
                    continue
                if child.is_alive():
                    if time.monotonic() - child.last_spawn_at > 60.0:
                        child.backoff_idx = 0
                    continue

                rc = child.proc.returncode if child.proc else None
                log.warning(
                    "supervisor_child_exited",
                    user_id=child.user.id, kind=child.kind, returncode=rc,
                )
                child._close_log()
                child.record_failure(max_fail)
                if child.degraded:
                    continue
                delay = child.next_backoff(backoff)
                log.info(
                    "supervisor_restart_scheduled",
                    user_id=child.user.id, kind=child.kind, delay_seconds=delay,
                )
                # Sleep here is OK; the supervisor loop is otherwise idle.
                time.sleep(delay)
                try:
                    child.spawn()
                except Exception as exc:
                    log.error(
                        "supervisor_spawn_failed",
                        user_id=child.user.id, kind=child.kind, error=str(exc),
                    )
                    child.record_failure(max_fail)

    async def _reconcile(self) -> None:
        """Re-read users.yaml and align running set."""
        try:
            users = self._read_users()
        except Exception as exc:
            log.error("supervisor_reload_failed", error=str(exc))
            return
        wanted = {u.id: u for u in users.enabled_users()}

        # 1) Stop agent children not in wanted set, or whose port changed.
        #    A port change cascades: the consolidator's --mcp-url embeds the
        #    port, so it must restart too.
        for user_id, child in list(self._children.items()):
            if user_id not in wanted:
                log.info("supervisor_reload_remove", user_id=user_id)
                child.terminate()
                self._children.pop(user_id, None)
                self._terminate_consolidator(user_id)
                continue
            new_def = wanted[user_id]
            if new_def.port != child.user.port:
                log.info(
                    "supervisor_reload_port_change",
                    user_id=user_id,
                    old=child.user.port,
                    new=new_def.port,
                )
                child.terminate()
                self._children.pop(user_id, None)
                # Port shifts ⇒ consolidator's --mcp-url is stale; drop it so
                # step 3 below respawns with the new port.
                self._terminate_consolidator(user_id)

        # 2) Start children that should be running but aren't. Process eager
        #    init results as they arrive so one slow/bad palace does not block
        #    unrelated users from getting a worker during SIGHUP reconcile.
        spawn_candidates = [
            u for u in wanted.values()
            if _agent_child_needs_spawn(self._children.get(u.id))
        ]
        if self._eager_init:
            by_id = {u.id: u for u in spawn_candidates}
            init_tasks = [
                asyncio.create_task(self._init_user(u)) for u in spawn_candidates
            ]
            for task in asyncio.as_completed(init_tasks):
                user_id, err = await task
                if err is not None:
                    log.error(
                        "supervisor_palace_init_failed",
                        user_id=user_id,
                        error=err,
                    )
                    continue
                user_def = by_id.get(user_id)
                if user_def is not None:
                    self._ensure_agent_child(user_def)
        else:
            for user_def in spawn_candidates:
                self._ensure_agent_child(user_def)

        # Refresh live child definitions even when they did not need a spawn.
        for user_id, user_def in wanted.items():
            existing = self._children.get(user_id)
            if existing is not None and existing.is_alive() and not existing.degraded:
                existing.user = user_def

        # 3) Reconcile consolidators against the current wanted set:
        #    - removed user / disabled flag → terminate
        #    - newly enabled → spawn
        #    - config field change (interval / window / etc.) → restart
        ready_user_ids = {
            user_id
            for user_id, child in self._children.items()
            if user_id in wanted and child.is_alive() and not child.degraded
        }
        for user_id, c_child in list(self._consolidators.items()):
            wanted_def = wanted.get(user_id)
            if (
                wanted_def is None
                or not wanted_def.consolidator_enabled()
                or user_id not in ready_user_ids
            ):
                log.info("supervisor_reload_consolidator_remove", user_id=user_id)
                self._terminate_consolidator(user_id)
                continue
            if wanted_def.consolidator != c_child.user.consolidator:
                log.info(
                    "supervisor_reload_consolidator_config_change", user_id=user_id,
                )
                self._terminate_consolidator(user_id)
        for user_id, user_def in wanted.items():
            if not user_def.consolidator_enabled():
                continue
            if user_id not in ready_user_ids:
                continue
            # Refresh the stored UserEntry on the existing child or spawn fresh.
            existing = self._consolidators.get(user_id)
            if existing is not None and existing.is_alive():
                existing.user = user_def
                continue
            self._spawn_consolidator(user_def)

    def _ensure_agent_child(self, user_def: UserEntry) -> None:
        existing = self._children.get(user_def.id)
        if existing is not None and existing.is_alive() and not existing.degraded:
            existing.user = user_def
            return
        if existing is not None:
            if existing.is_alive():
                existing.terminate()
            else:
                existing._close_log()
            self._children.pop(user_def.id, None)
        child = _Child(user_def, self._palace_for(user_def), self._log_root)
        try:
            child.spawn()
            self._children[user_def.id] = child
        except Exception as exc:
            log.error(
                "supervisor_reload_spawn_failed",
                user_id=user_def.id,
                error=str(exc),
            )

    def _terminate_consolidator(self, user_id: str) -> None:
        c = self._consolidators.pop(user_id, None)
        if c is not None:
            c.terminate(grace_seconds=10.0)

    # -------------------- signal handlers --------------------

    def request_reload(self) -> None:
        self._reload_event.set()

    def request_stop(self) -> None:
        self._stop_event.set()


def _agent_child_needs_spawn(child: _Child | None) -> bool:
    return child is None or child.degraded or not child.is_alive()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eidolon-memory-supervisor",
        description="Multi-user agent_runner supervisor (D1)",
    )
    parser.add_argument(
        "--users-file",
        default="",
        help="Legacy/testing override for users.yaml path.",
    )
    parser.add_argument(
        "--no-init",
        action="store_true",
        help="Skip eager mempalace init; rely on agent_runner's lazy init.",
    )
    parser.add_argument(
        "--admin-host",
        default="",
        help="Bind host for the admin HTTP surface (default settings.supervisor.admin_http_host).",
    )
    parser.add_argument(
        "--admin-port",
        type=int,
        default=0,
        help="Bind port for the admin HTTP surface (default settings.supervisor.admin_http_port).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_memory_settings()
    users_path = (
        resolve_users_file_path(settings, path=args.users_file)
        if args.users_file
        else None
    )

    supervisor = Supervisor(
        settings,
        users_path,
        eager_init=(not args.no_init) and settings.supervisor.eager_init,
    )

    # Admin HTTP control surface — runs in the same asyncio loop as the
    # reconcile/check passes so HTTP handlers can directly drive supervisor
    # state transitions without cross-process signaling.
    import uvicorn

    user_admin = UserAdmin(supervisor)
    admin_app = build_admin_api(user_admin)
    admin_host = (args.admin_host or settings.supervisor.admin_http_host).strip() or "127.0.0.1"
    admin_port = args.admin_port or settings.supervisor.admin_http_port

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig, handler in (
            (signal.SIGINT, supervisor.request_stop),
            (signal.SIGTERM, supervisor.request_stop),
            (signal.SIGHUP, supervisor.request_reload),
        ):
            try:
                loop.add_signal_handler(sig, handler)
            except (NotImplementedError, RuntimeError):
                pass

        # Run supervisor's reconcile loop AND the admin HTTP server in the
        # same loop. When SIGTERM fires:
        #   * supervisor.run() observes ``_stop_event`` and exits cleanly
        #   * we explicitly tell uvicorn to shut down via ``server.should_exit``
        # asyncio.gather then unblocks and the process exits.
        admin_server = uvicorn.Server(
            uvicorn.Config(
                admin_app,
                host=admin_host,
                port=admin_port,
                log_level="warning",  # keep INFO noise for the supervisor itself
                access_log=False,
            )
        )

        sv_task = asyncio.create_task(supervisor.run(), name="supervisor_run")
        api_task = asyncio.create_task(admin_server.serve(), name="admin_http_serve")

        # Watchdog: if either task finishes first (supervisor stop → graceful;
        # uvicorn crash → bad), cancel the other so we don't dangle.
        done, pending = await asyncio.wait(
            {sv_task, api_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task is sv_task:
                # Graceful shutdown path: tell uvicorn to exit and await.
                admin_server.should_exit = True
            else:
                # Admin HTTP died unexpectedly; force supervisor down too.
                log.error("supervisor_admin_http_exited_unexpectedly")
                supervisor.request_stop()
        for task in pending:
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with __import__("contextlib").suppress(asyncio.CancelledError):
                    await task
        # Surface exceptions from the originally-finished task last so the
        # process exit code reflects the real failure if there was one.
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
