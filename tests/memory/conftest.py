"""eidolon.memory 测试共享 fixtures。"""

from __future__ import annotations

import pytest

from eidolon.memory.config.memory_settings import (
    reset_memory_settings_cache,
)


@pytest.fixture(autouse=True)
def _reset_default_memory_settings_cache() -> None:
    """每个用例开始前清空默认路径缓存，避免 env 或文件与上一用例串味。"""
    reset_memory_settings_cache()
    yield


# A real PostgreSQL for the shared-storage paths, started from a wheel so an
# ordinary run needs no server installed. See postgres_fixture for why.
from tests.memory.postgres_fixture import (  # noqa: E402
    postgres_dsn,
    postgres_pool,
)

__all__ = ["postgres_dsn", "postgres_pool"]
