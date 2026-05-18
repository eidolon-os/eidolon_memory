"""Worker bumps generation before ACK."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.entrypoints.worker import process_turn_message


@pytest.mark.asyncio
async def test_process_turn_bumps_before_ack(tmp_path) -> None:
    gen_file = tmp_path / "palace_generation"
    turn = ConversationTurnPayload(
        turn_id="t1",
        session_id="s1",
        user_id="u1",
        user_text="hi",
        assistant_text="hello",
        timestamp="2026-05-18T12:00:00+00:00",
    )
    msg = MagicMock()
    msg.data = turn.model_dump_json().encode("utf-8")
    msg.metadata = None
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()

    steward = MagicMock()
    steward.handle_turn = AsyncMock()
    backend = MagicMock()
    settings = MagicMock()
    settings.nats.worker_max_deliveries = 3
    settings.nats.dlq_log_path = str(tmp_path / "dlq.jsonl")

    await process_turn_message(
        msg,
        steward=steward,
        backend=backend,
        gen_path=gen_file,
        settings=settings,
        max_deliveries=3,
    )

    assert gen_file.is_file()
    steward.handle_turn.assert_awaited_once()
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
