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
import os
import signal
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eidolon.memory.config.memory_settings import (
    MemorySettings,
    get_memory_settings,
    resolve_log_dir,
)
from eidolon.memory.config.palace_directory import resolve_palace_for_user
from eidolon.memory.config.users import (
    UserEntry,
    UsersConfig,
    ensure_users_yaml_exists,
    load_users_config,
    resolve_users_file_path,
)
from eidolon.memory.infrastructure.palace_init import (
    PalaceInitError,
    ensure_palace_initialized,
)
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_AGENT_CLI = "eidolon-memory-agent"
_DEGRADED_MIN_INTERVAL = 60.0  # seconds — rolling failure window


def _agent_cli_argv(user: UserEntry, palace_path: Path) -> list[str]:
    argv = [_AGENT_CLI, "--user-id", user.id, "--port", str(user.port)]
    if user.palace_path:
        argv += ["--palace-path", str(palace_path)]
    return argv


def _open_child_log(log_root: Path, user_id: str) -> tuple[Path, "subprocess._FILE"]:
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / f"agent_{user_id}.log"
    fh = log_path.open("ab", buffering=0)
    return log_path, fh


class _Child:
    """Represents one supervised agent_runner subprocess."""

    def __init__(self, user: UserEntry, palace_path: Path, log_root: Path) -> None:
        self.user = user
        self.palace_path = palace_path
        self.log_root = log_root
        self.proc: subprocess.Popen | None = None
        self.log_path: Path | None = None
        self._log_fh = None
        self.failure_times: deque[float] = deque()
        self.backoff_idx: int = 0
        self.degraded: bool = False
        self.last_spawn_at: float = 0.0

    def spawn(self) -> None:
        self.log_path, self._log_fh = _open_child_log(self.log_root, self.user.id)
        argv = _agent_cli_argv(self.user, self.palace_path)
        log.info(
            "supervisor_spawn",
            user_id=self.user.id,
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
        log.info("supervisor_terminate", user_id=self.user.id, pid=self.proc.pid)
        try:
            self.proc.terminate()
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            log.warning("supervisor_kill", user_id=self.user.id, pid=self.proc.pid)
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
                user_id=self.user.id,
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
        users_path: Path,
        *,
        eager_init: bool | None = None,
    ) -> None:
        self._settings = settings
        self._users_path = users_path
        self._log_root = resolve_log_dir(settings)
        self._eager_init = (
            settings.supervisor.eager_init if eager_init is None else eager_init
        )
        self._children: dict[str, _Child] = {}
        self._reload_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._init_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="supervisor-init"
        )

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

        loop = asyncio.get_running_loop()

        def _init_one(u: UserEntry) -> tuple[str, str | None]:
            try:
                ensure_palace_initialized(u.id, self._palace_for(u))
                return u.id, None
            except PalaceInitError as exc:
                return u.id, str(exc)

        tasks = [
            loop.run_in_executor(self._init_pool, _init_one, u) for u in users
        ]
        results = await asyncio.gather(*tasks)
        ok: set[str] = set()
        for user_id, err in results:
            if err is None:
                ok.add(user_id)
            else:
                log.error("supervisor_palace_init_failed", user_id=user_id, error=err)
        return ok

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
        ok = await self._init_users_parallel(enabled)
        for user in enabled:
            if user.id not in ok:
                continue  # init failed; skipped
            child = _Child(user, self._palace_for(user), self._log_root)
            child.spawn()
            self._children[user.id] = child

    async def stop(self) -> None:
        log.info("supervisor_stop_begin", n=len(self._children))
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
        backoff = self._settings.supervisor.restart_backoff_seconds
        max_fail = self._settings.supervisor.max_failures_per_minute
        for child in list(self._children.values()):
            if child.degraded:
                continue
            if child.is_alive():
                # Reset backoff if process has been up for a while
                if time.monotonic() - child.last_spawn_at > 60.0:
                    child.backoff_idx = 0
                continue

            # Child died — figure out backoff and restart
            rc = child.proc.returncode if child.proc else None
            log.warning("supervisor_child_exited", user_id=child.user.id, returncode=rc)
            child.record_failure(max_fail)
            if child.degraded:
                continue
            delay = child.next_backoff(backoff)
            log.info(
                "supervisor_restart_scheduled",
                user_id=child.user.id,
                delay_seconds=delay,
            )
            # Sleep here is OK; the supervisor loop is otherwise idle.
            # We block briefly to honor backoff per-child.
            time.sleep(delay)
            try:
                child.spawn()
            except Exception as exc:
                log.error(
                    "supervisor_spawn_failed",
                    user_id=child.user.id,
                    error=str(exc),
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
        wanted_init_pool = [u for u in wanted.values() if u.id not in self._children]
        ok = await self._init_users_parallel(wanted_init_pool)

        # 1) Stop children not in wanted set, or whose port changed
        for user_id, child in list(self._children.items()):
            if user_id not in wanted:
                log.info("supervisor_reload_remove", user_id=user_id)
                child.terminate()
                self._children.pop(user_id, None)
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

        # 2) Start children that should be running but aren't
        for user_id, user_def in wanted.items():
            if user_id in self._children and self._children[user_id].is_alive():
                continue
            if self._eager_init and user_id not in ok:
                continue
            child = _Child(user_def, self._palace_for(user_def), self._log_root)
            try:
                child.spawn()
                self._children[user_id] = child
            except Exception as exc:
                log.error(
                    "supervisor_reload_spawn_failed",
                    user_id=user_id,
                    error=str(exc),
                )

    # -------------------- signal handlers --------------------

    def request_reload(self) -> None:
        self._reload_event.set()

    def request_stop(self) -> None:
        self._stop_event.set()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eidolon-memory-supervisor",
        description="Multi-user agent_runner supervisor (D1)",
    )
    parser.add_argument(
        "--users-file",
        default="",
        help="Override users.yaml path (env EIDOLON_MEMORY_USERS_YAML / settings).",
    )
    parser.add_argument(
        "--no-init",
        action="store_true",
        help="Skip eager mempalace init; rely on agent_runner's lazy init.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_memory_settings()
    users_path = resolve_users_file_path(settings, path=args.users_file or None)

    # Bootstrap: seed users.yaml from the bundled template on first start.
    try:
        seeded = ensure_users_yaml_exists(users_path)
    except FileNotFoundError as exc:
        log.error("supervisor_users_template_missing", error=str(exc))
        sys.exit(2)
    if seeded:
        log.info("supervisor_users_yaml_created", path=str(users_path))

    supervisor = Supervisor(
        settings,
        users_path,
        eager_init=(not args.no_init) and settings.supervisor.eager_init,
    )

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
        await supervisor.run()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
