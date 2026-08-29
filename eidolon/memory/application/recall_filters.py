"""Filters for voice recall (session de-duplication)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eidolon_memory_contracts import USER_CONFIRMED_ROOM_PREFIX

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.wire import MemoryWireRecord


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def filter_voice_recall_hits(
    hits: list[MemoryWireRecord],
    settings: MemorySettings,
    *,
    session_id: str = "",
    user_utterance: str = "",
) -> list[MemoryWireRecord]:
    """Drop recent / same-session fragments to avoid duplicating LiveKit chat history."""
    cutoff: datetime | None = None
    minutes = settings.recall.exclude_recent_minutes
    if minutes > 0:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)

    utter_norm = user_utterance.strip().lower()
    out: list[MemoryWireRecord] = []
    for rec in hits:
        metadata = rec.metadata or {}
        room = str(metadata.get("room") or rec.key or "")
        user_confirmed = (
            metadata.get("source") == "user-confirmed"
            or room.startswith(USER_CONFIRMED_ROOM_PREFIX)
        )
        # An explicit memory tool write is not a transcript echo. It promises
        # write-after-visible semantics and must remain recallable even when it
        # was confirmed in this session moments ago. Freshness/session
        # suppression applies only to naturally extracted conversation turns.
        if user_confirmed:
            out.append(rec)
            continue
        if settings.recall.exclude_current_session and session_id:
            if str(metadata.get("session_id", "")) == session_id:
                continue
            if str(metadata.get("source_turn_id", "")).startswith(session_id):
                continue

        if cutoff is not None:
            too_recent = False
            for key in ("filed_at", "occurred_at"):
                ts = _parse_iso(str(metadata.get(key, "")))
                if ts is not None and ts >= cutoff:
                    too_recent = True
                    break
            if too_recent:
                continue

        if utter_norm and str(rec.value).strip().lower() == utter_norm:
            continue

        out.append(rec)
    return out
