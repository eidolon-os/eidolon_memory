"""CPU thread auto-configuration."""

from __future__ import annotations

import os

import pytest

from eidolon.memory.config.memory_settings import MemorySettings, ReadRuntimeConfig
from eidolon.memory.infrastructure.cpu_env import (
    apply_cpu_thread_env,
    recommend_max_wing_parallel,
    recommend_omp_threads,
)


def _settings() -> MemorySettings:
    return MemorySettings(
        runtime=__import__(
            "eidolon.memory.config.memory_settings", fromlist=["RuntimeConfig"]
        ).RuntimeConfig(
            read=ReadRuntimeConfig(max_wing_parallel=0, omp_num_threads=0),
        ),
    )


def test_recommend_omp_livekit_positive():
    n = recommend_omp_threads(_settings(), role="livekit")
    assert 1 <= n <= 4


def test_explicit_omp_in_yaml():
    s = _settings()
    s.runtime.read.omp_num_threads = 5
    assert recommend_omp_threads(s, role="livekit") == 5


def test_apply_cpu_env_sets_when_unset(monkeypatch):
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        monkeypatch.delenv(key, raising=False)
    n = apply_cpu_thread_env(_settings(), role="mcp")
    assert os.environ.get("OMP_NUM_THREADS") == str(n)


def test_recommend_max_wing_parallel_auto():
    s = _settings()
    p = recommend_max_wing_parallel(s, role="livekit")
    assert 1 <= p <= 4


def test_embed_query_handles_ndarray_vector(monkeypatch):
    import numpy as np

    from eidolon.memory.adapters import mempalace_query_embedding as qe

    class _EF:
        def __call__(self, texts):
            return [np.array([0.1, 0.2, 0.3], dtype=np.float32)]

    monkeypatch.setattr(
        "mempalace.embedding.get_embedding_function",
        lambda: _EF(),
    )
    qe.clear_embedding_cache()
    vec = qe.embed_query_vector("hello-ndarray")
    assert len(vec) == 3
    assert vec[0] == pytest.approx(0.1)
    assert vec[1] == pytest.approx(0.2)
    assert vec[2] == pytest.approx(0.3)
