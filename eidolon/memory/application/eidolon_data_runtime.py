"""Runtime composition between ``eidolon_data`` and eidolon_memory.

``eidolon_data`` owns the sovereignty schema and defines ``MemoryEnginePort``.
This module lives in ``eidolon_memory`` because only the memory project should
know how to construct MemPalace-backed implementations of that port.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eidolon_data import DataSettings, DataStore

from eidolon.memory.adapters.eidolon_data_engine import EidolonDataMemoryEngine
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.config import (
    MemorySettings,
    get_memory_settings,
    resolve_palace_for_memory_space,
)
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.support.logging import get_logger

_log = get_logger(__name__)


class EidolonDataMemoryFanoutAuditSink:
    """Best-effort audit sink closing the agent→memory fanout handshake.

    The agent emits ``eidolon.memory.fanout.status`` (published to NATS); memory
    confirms the other half with ``memory.fanout.absorbed`` / ``.rejected`` keyed
    on the same ``turn`` (subject_type="turn"), so the audit view shows
    end-to-end delivery rather than only "we tried". owner/companion are read
    from the turn envelope metadata the agent stamped. Never raises into the turn
    hot path — a failed audit write is logged and swallowed.
    """

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def record_absorbed(
        self, turn: Any, *, trace_id: str, should_write: bool, fragments: int, triples: int
    ) -> None:
        await self._emit(
            "memory.fanout.absorbed",
            turn,
            trace_id,
            {"should_write": should_write, "fragments": fragments, "triples": triples},
        )

    async def record_rejected(
        self, turn: Any, *, trace_id: str, reason: str, deliveries: int
    ) -> None:
        await self._emit(
            "memory.fanout.rejected",
            turn,
            trace_id,
            {"reason": reason[:240], "deliveries": deliveries},
        )

    async def _emit(self, event_type: str, turn: Any, trace_id: str, payload: dict) -> None:
        context = getattr(turn, "context", None)
        owner_id = str(getattr(context, "owner_id", "") or "")
        if not owner_id:
            return  # no owner scope on the turn context — skip audit rather than guess
        companion_id = str(getattr(context, "companion_id", "") or "") or None
        memory_space_id = getattr(context, "memory_space_id", "") if context is not None else ""
        try:
            await self._data_store.events.record_event(
                event_type=event_type,
                owner_id=owner_id,
                companion_id=companion_id,
                subject_type="turn",
                subject_id=turn.turn_id,
                actor_type="memory",
                trace_id=trace_id or None,
                payload_json={"memory_space_id": memory_space_id, **payload},
            )
        except Exception as exc:  # noqa: BLE001 - audit must never break the turn path
            _log.warning(
                "memory_fanout_audit_failed",
                event_type=event_type,
                turn_id=getattr(turn, "turn_id", None),
                error=str(exc),
            )


def build_eidolon_data_memory_engine(
    *,
    backend: MemoryBackend | None = None,
    settings: MemorySettings | None = None,
    memory_space_id: str | None = None,
    palace_path: str | Path | None = None,
    locked: bool = True,
) -> EidolonDataMemoryEngine:
    """Build the ``eidolon_data`` memory port implementation.

    Tests and embedded runtimes may pass an already-constructed ``backend``.
    Production callers can pass ``settings`` plus either ``palace_path`` or a
    ``memory_space_id`` so this helper can construct the MemPalace backend.
    """

    resolved_backend = backend
    if resolved_backend is None:
        resolved_settings = settings or get_memory_settings()
        resolved_palace = _resolve_palace_path(
            resolved_settings,
            memory_space_id=memory_space_id,
            palace_path=palace_path,
        )
        resolved_backend = MemPalacePythonBackend(
            resolved_settings, str(resolved_palace), memory_space_id=memory_space_id
        )

    if locked and not isinstance(resolved_backend, LockedBackend):
        resolved_backend = LockedBackend(resolved_backend)
    return EidolonDataMemoryEngine(resolved_backend)


def open_eidolon_data_store(
    *,
    data_settings: DataSettings | None = None,
    backend: MemoryBackend | None = None,
    settings: MemorySettings | None = None,
    memory_space_id: str | None = None,
    palace_path: str | Path | None = None,
    locked: bool = True,
) -> DataStore:
    """Open ``DataStore`` with an eidolon_memory-backed memory engine."""

    engine = build_eidolon_data_memory_engine(
        backend=backend,
        settings=settings,
        memory_space_id=memory_space_id,
        palace_path=palace_path,
        locked=locked,
    )
    return DataStore.open(data_settings, memory_engine=engine)


def _resolve_palace_path(
    settings: MemorySettings,
    *,
    memory_space_id: str | None,
    palace_path: str | Path | None,
) -> Path:
    if palace_path is not None:
        return Path(palace_path).expanduser().resolve()
    if not memory_space_id:
        raise ValueError("memory_space_id or palace_path is required when backend is not supplied")
    return resolve_palace_for_memory_space(settings, memory_space_id)


__all__ = [
    "EidolonDataMemoryFanoutAuditSink",
    "build_eidolon_data_memory_engine",
    "open_eidolon_data_store",
]
