"""Auto-configure ONNX/BLAS thread counts for the current machine and role."""

from __future__ import annotations

import os
import platform
import sys
from typing import Literal

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

Role = Literal["livekit", "mcp", "worker", "default"]

_OMP_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def cpu_core_count() -> int:
    return max(1, os.cpu_count() or 4)


def recommend_omp_threads(
    settings: MemorySettings | None = None,
    *,
    role: Role = "default",
) -> int:
    """Pick ONNX-friendly thread count; respect explicit YAML/env overrides."""
    if settings is not None:
        explicit = settings.runtime.read.omp_num_threads
        if explicit > 0:
            return explicit

    for key in _OMP_VARS:
        raw = os.environ.get(key, "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)

    cores = cpu_core_count()
    parallel = 4
    if settings is not None:
        parallel = max(1, settings.runtime.read.max_wing_parallel)

    # Apple Silicon: leave headroom for LiveKit audio + asyncio.
    is_apple_arm = sys.platform == "darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }

    if role == "livekit":
        if is_apple_arm:
            # M-series: 2 threads often best for parallel wing embed + audio.
            return max(1, min(3, 2 if cores >= 8 else 2))
        return max(1, min(3, parallel))

    if role == "worker":
        # Writes are lighter on ONNX; avoid competing with recall.
        return 1

    if role == "mcp":
        if is_apple_arm:
            return max(1, min(4, cores // 4 or 1))
        return max(1, min(4, cores // 2))

    # default / warm
    if is_apple_arm:
        return max(2, min(4, cores // 4))
    return max(1, min(4, cores // 2))


def recommend_max_wing_parallel(settings: MemorySettings, *, role: Role = "livekit") -> int:
    """Optional cap when max_wing_parallel is 0 (auto)."""
    if settings.runtime.read.max_wing_parallel > 0:
        return settings.runtime.read.max_wing_parallel
    omp = recommend_omp_threads(settings, role=role)
    if role == "livekit":
        return max(1, min(4, omp + 1))
    return max(1, min(4, cpu_core_count() // 2))


def apply_cpu_thread_env(
    settings: MemorySettings | None = None,
    *,
    role: Role = "default",
    force: bool = False,
) -> int:
    """Set BLAS/ONNX thread env vars if not already set (unless ``force``)."""
    n = recommend_omp_threads(settings, role=role)
    for key in _OMP_VARS:
        if force or not os.environ.get(key):
            os.environ[key] = str(n)
    log.info(
        "cpu_thread_env_applied",
        role=role,
        omp_num_threads=n,
        cpu_cores=cpu_core_count(),
        machine=platform.machine(),
        forced=force,
    )
    return n
