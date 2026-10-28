from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.api import DiscardPolicy
from nats.js.errors import NotFoundError

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.nats_stream import (
    ensure_memory_stream,
    stream_config_for_settings,
)


@pytest.mark.parametrize("existing", [False, True])
async def test_stream_capacity_policy_is_applied_or_failure_is_visible(existing):
    settings = MemorySettings()
    assert stream_config_for_settings(settings).discard == DiscardPolicy.NEW
    js = SimpleNamespace(stream_info=AsyncMock(), add_stream=AsyncMock(), update_stream=AsyncMock())
    if not existing:
        js.stream_info.side_effect = NotFoundError
    await ensure_memory_stream(js, settings)
    writer = js.update_stream if existing else js.add_stream
    writer.assert_awaited_once()
    assert writer.call_args.args[0].discard == DiscardPolicy.NEW
    writer.side_effect = PermissionError("cannot apply policy")
    with pytest.raises(PermissionError):
        await ensure_memory_stream(js, settings)


async def test_unavailable_stream_lookup_does_not_try_to_create_a_replacement():
    js = SimpleNamespace(stream_info=AsyncMock(side_effect=TimeoutError), add_stream=AsyncMock())
    with pytest.raises(TimeoutError):
        await ensure_memory_stream(js, MemorySettings())
    js.add_stream.assert_not_awaited()
