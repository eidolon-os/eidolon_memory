"""Double-buffered read session: generation bumps do not block LiveKit hot path."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.chroma_refresh import close_mempalace_palace, pop_mempalace_client_cache
from eidolon.memory.infrastructure.cpu_env import recommend_omp_threads
from eidolon.memory.infrastructure.palace_generation import (
    GenerationInfo,
    read_generation,
    resolve_generation_path,
)
from eidolon.memory.support.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


def _build_backend(settings: MemorySettings, palace_path: str) -> MemPalacePythonBackend:
    pop_mempalace_client_cache(palace_path)
    return MemPalacePythonBackend(settings, palace_path)


def _warm_backend_sync(backend: MemPalacePythonBackend, wing_id: str) -> None:
    import asyncio as _asyncio

    async def _once() -> None:
        await backend.search("warmup", wing=wing_id, n_results=1)

    _asyncio.run(_once())


class PalaceReadSession:
    """Tracks palace generation; hot path uses active backend while staging reloads in background."""

    def __init__(
        self,
        settings: MemorySettings,
        palace_path: str,
        *,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._settings = settings
        self._palace = palace_path
        self._gen_path = resolve_generation_path(
            palace_path,
            settings.runtime.read.generation_path,
        )
        self._seen_generation = read_generation(self._gen_path).generation
        self._active = MemPalacePythonBackend(settings, palace_path)
        self._staging: MemPalacePythonBackend | None = None
        self._staging_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        threads = settings.runtime.read.search_executor_threads
        if threads <= 0:
            threads = max(2, recommend_omp_threads(settings, role="livekit") + 1)
        self._executor = executor or ThreadPoolExecutor(
            max_workers=threads,
            thread_name_prefix="eidolon-memory-search",
        )

    @property
    def executor(self) -> ThreadPoolExecutor:
        return self._executor

    @property
    def generation_path(self) -> str:
        return str(self._gen_path)

    @property
    def palace_path(self) -> str:
        return self._palace

    def current_generation(self) -> int:
        return self._seen_generation

    async def active_backend(self) -> MemPalacePythonBackend:
        await self.ensure_fresh()
        return self._active

    async def ensure_fresh(self) -> GenerationInfo:
        """If generation changed, start background staging; never block on full reload."""
        info = read_generation(self._gen_path)
        if info.generation == self._seen_generation:
            return info
        if self._staging_task is None or self._staging_task.done():
            self._staging_task = asyncio.create_task(self._run_staging(info.generation))
        return info

    async def _run_staging(self, target_generation: int) -> None:
        if not self._settings.runtime.read.double_buffer_staging:
            await self._swap_active_sync(target_generation)
            return
        loop = asyncio.get_running_loop()
        try:
            staging = await loop.run_in_executor(
                self._executor,
                _build_backend,
                self._settings,
                self._palace,
            )
            wing = _pick_warm_wing(self._settings)
            await loop.run_in_executor(
                self._executor,
                _warm_backend_sync,
                staging,
                wing,
            )
            async with self._lock:
                self._active = staging
                self._seen_generation = target_generation
            log.info(
                "palace_read_session_staging_ready",
                generation=target_generation,
                palace=self._palace,
            )
        except Exception as exc:
            log.warning(
                "palace_read_session_staging_failed",
                generation=target_generation,
                error=str(exc),
            )

    async def _swap_active_sync(self, target_generation: int) -> None:
        loop = asyncio.get_running_loop()
        try:
            backend = await loop.run_in_executor(
                self._executor,
                _build_backend,
                self._settings,
                self._palace,
            )
            async with self._lock:
                self._active = backend
                self._seen_generation = target_generation
        except Exception as exc:
            log.warning("palace_read_session_sync_swap_failed", error=str(exc))

    async def background_reconcile(self) -> None:
        """Optional heal after LiveKit fail-fast (next turn)."""
        if not self._settings.runtime.read.background_reconcile:
            return
        info = read_generation(self._gen_path)
        if info.generation != self._seen_generation:
            await self.ensure_fresh()

    def close(self) -> None:
        close_mempalace_palace(self._palace)
        self._executor.shutdown(wait=False, cancel_futures=True)


def _pick_warm_wing(settings: MemorySettings) -> str:
    wings = settings.recall.voice_wings or [
        w.id for w in settings.wings if w.id != "Wing_Privacy"
    ]
    return wings[0] if wings else settings.wings[0].id
