"""Lifecycle for per-user ``eidolon-memory-agent`` subprocesses.

The admin server can spawn / stop agent_runner processes it owns. Externally
started agents (supervisor / shell) are visible via the port probe but the
admin will not signal them — only its own subprocess registry can be stopped
from the UI.

Spawning is done via ``subprocess.Popen`` so the child is a fresh Python
interpreter — no chromadb state is inherited from the admin process (D1
single-owner rule for palace files).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def _resolve_agent_cli() -> str:
    cand = Path(sys.executable).parent / "eidolon-memory-agent"
    if cand.is_file():
        return str(cand)
    import shutil

    found = shutil.which("eidolon-memory-agent")
    if not found:
        msg = "eidolon-memory-agent CLI not found in venv or PATH"
        raise FileNotFoundError(msg)
    return found


@dataclass
class ManagedAgent:
    user_id: str
    port: int
    pid: int
    log_path: str
    started_at: float


class AgentProcessManager:
    """Tracks ``user_id → subprocess.Popen`` for agents spawned by admin."""

    def __init__(self, *, log_dir: Path | None = None) -> None:
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._meta: dict[str, ManagedAgent] = {}
        self._log_dir = log_dir or (Path.home() / "eidolon" / "logs" / "admin_spawned")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def is_managed(self, user_id: str) -> bool:
        proc = self._procs.get(user_id)
        if proc is None:
            return False
        if proc.poll() is not None:
            # died — drop bookkeeping
            self._procs.pop(user_id, None)
            self._meta.pop(user_id, None)
            return False
        return True

    def status(self, user_id: str) -> ManagedAgent | None:
        if not self.is_managed(user_id):
            return None
        return self._meta.get(user_id)

    def all(self) -> list[ManagedAgent]:
        return [
            m for uid, m in list(self._meta.items()) if self.is_managed(uid)
        ]

    async def start(self, *, user_id: str, port: int) -> ManagedAgent:
        async with self._lock:
            if self.is_managed(user_id):
                meta = self._meta[user_id]
                log.info("agent_manager_already_running", user_id=user_id, pid=meta.pid)
                return meta
            cli = _resolve_agent_cli()
            log_path = self._log_dir / f"{user_id}.log"
            log_fd = log_path.open("ab", buffering=0)
            try:
                proc = subprocess.Popen(
                    [cli, "--user-id", user_id, "--port", str(port)],
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env={**os.environ},
                )
            finally:
                log_fd.close()
            meta = ManagedAgent(
                user_id=user_id,
                port=port,
                pid=proc.pid,
                log_path=str(log_path),
                started_at=time.time(),
            )
            self._procs[user_id] = proc
            self._meta[user_id] = meta
            log.info(
                "agent_manager_spawned",
                user_id=user_id,
                port=port,
                pid=proc.pid,
                log=str(log_path),
            )
            return meta

    async def stop(
        self,
        user_id: str,
        *,
        sigterm_grace_seconds: float = 8.0,
    ) -> bool:
        async with self._lock:
            proc = self._procs.get(user_id)
            if proc is None:
                return False
            if proc.poll() is not None:
                self._procs.pop(user_id, None)
                self._meta.pop(user_id, None)
                return False
            log.info("agent_manager_stop_sigterm", user_id=user_id, pid=proc.pid)
            try:
                proc.terminate()
            except ProcessLookupError:
                self._procs.pop(user_id, None)
                self._meta.pop(user_id, None)
                return True
            try:
                await asyncio.to_thread(proc.wait, sigterm_grace_seconds)
            except subprocess.TimeoutExpired:
                log.warning(
                    "agent_manager_stop_sigkill", user_id=user_id, pid=proc.pid
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.to_thread(proc.wait, 3.0)
                except subprocess.TimeoutExpired:
                    pass
            self._procs.pop(user_id, None)
            self._meta.pop(user_id, None)
            return True

    async def stop_all(self) -> None:
        for uid in list(self._procs):
            try:
                await self.stop(uid)
            except Exception as exc:
                log.warning("agent_manager_stop_failed", user_id=uid, error=str(exc))
