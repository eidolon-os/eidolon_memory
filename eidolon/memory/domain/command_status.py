"""Domain language for observable asynchronous memory writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

CommandStatus = Literal["accepted", "retrying", "applied", "failed"]
TERMINAL_COMMAND_STATUSES = frozenset({"applied", "failed"})


@dataclass(frozen=True, slots=True)
class CommandStatusRecord:
    request_id: str
    kind: str
    status: CommandStatus
    resource_id: str | None
    error: str | None
    attempts: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CommandStatusStats:
    total: int
    accepted: int
    retrying: int
    applied: int
    failed: int
    database_bytes: int
    retention_days: int
    max_records: int
    oldest_active_at: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
