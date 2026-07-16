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
