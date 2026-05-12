"""Bus envelope schemas for NATS memory RPC (same wire shape as eidolon.agent.shared.bus)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import Field

from eidolon.memory.support.model_base import BaseEidolonModel


class BusHeader(BaseEidolonModel):
    """Fixed header attached to every bus message."""

    msg_id: str = Field(default_factory=lambda: uuid4().hex)
    msg_type: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = ""
    target: str = ""


class BusEnvelope(BaseEidolonModel):
    """Unified envelope for NATS bus messages."""

    header: BusHeader
    trace_id: str = Field(default_factory=lambda: uuid4().hex)
    payload: dict[str, Any] = {}
