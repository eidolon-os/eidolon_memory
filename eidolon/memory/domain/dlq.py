"""Domain records for the recoverable dead-letter operational ledger."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

DlqState = Literal["unresolved", "replaying", "replayed", "resolved"]


@dataclass(frozen=True, slots=True)
class DlqRecord:
    entry_id: str
    subject: str
    error: str
    deliveries: int
    state: DlqState
    payload_size: int
    payload_preview: str
    replay_attempts: int
    resolution_note: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DlqReplayItem:
    record: DlqRecord
    payload: bytes


@dataclass(frozen=True, slots=True)
class DlqStats:
    total: int
    unresolved: int
    replaying: int
    replayed: int
    resolved: int
    payload_bytes: int
    database_bytes: int
    oldest_unresolved_at: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
