"""Contextual recall filtering and ranking for multi-device memory."""

from __future__ import annotations

import json
from typing import Any, Protocol

from eidolon_memory_contracts import (
    OWNER_AUDIENCE,
    MemoryActorContext,
    readable_audiences,
)

from eidolon.memory.domain.wire import MemoryWireRecord


class RecallExtensionPolicy(Protocol):
    """Optional extension hook for contextual recall scoring."""

    def boost(
        self,
        record: MemoryWireRecord,
        *,
        context: MemoryActorContext,
        query: str,
    ) -> float: ...


def parse_extensions(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cached = metadata.get("_parsed_extensions")
    if isinstance(cached, dict):
        return cached
    raw = metadata.get("extensions") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            out[key] = value
    metadata["_parsed_extensions"] = out
    return out


class LocationRecallPolicy:
    """Boost records whose location extension is mentioned by the query."""

    def boost(
        self,
        record: MemoryWireRecord,
        *,
        context: MemoryActorContext,
        query: str,
    ) -> float:
        del context
        location = parse_extensions(record.metadata).get("location") or {}
        room = str(location.get("room") or "")
        if room and room in query:
            return 0.12
        return 0.0


class RecallPolicyRegistry:
    """Core scope policy plus optional extension scoring hooks."""

    def __init__(self) -> None:
        self._extension_policies: dict[str, RecallExtensionPolicy] = {}

    def register(self, namespace: str, policy: RecallExtensionPolicy) -> None:
        self._extension_policies[namespace] = policy

    @classmethod
    def default(cls) -> RecallPolicyRegistry:
        registry = cls()
        registry.register("location", LocationRecallPolicy())
        return registry

    def visible(
        self,
        record: MemoryWireRecord,
        *,
        context: MemoryActorContext,
        include_private: bool = False,
        every_audience: bool = False,
    ) -> bool:
        """Whether this caller may see this record.

        ``every_audience`` is for the one caller that is not a Companion: the
        Owner asking for their own copy. The audience axis says which of their
        Eidolons was *told* a thing, and narrowing that is not the same as
        hiding it from the person who narrowed it — so their export carries all
        of it and names the audience per record. Every other rule here still
        applies, because those are boundaries the Owner is inside, not above:
        privacy, space and device visibility are unchanged by this flag.
        """

        meta = record.metadata or {}
        if meta.get("wing") == "Wing_Privacy":
            return False
        privacy = str(meta.get("privacy", "")).lower()
        if privacy in {"do_not_recall"}:
            return False
        if privacy == "private" and not include_private:
            return False
        if str(meta.get("memory_space_id") or record.memory_space_id) != context.memory_space_id:
            return False
        # Which of the owner's companions may see this. A record written before
        # the field existed has no audience, and is treated as the owner layer —
        # the same default writes take, so an upgrade does not hide what was
        # already recalled.
        audience = str(meta.get("audience") or OWNER_AUDIENCE)
        if not every_audience and audience not in readable_audiences(
            context.companion_id
        ):
            return False
        visibility = str(meta.get("visibility") or "all_devices")
        source_device = str(meta.get("source_device_id") or "")
        target_device = str(meta.get("target_device_id") or "")
        if visibility == "private" and not include_private:
            return False
        if visibility == "current_device":
            return bool(context.device_id) and context.device_id in {
                source_device,
                target_device,
            }
        return True

    def rendered(
        self,
        record: MemoryWireRecord,
        *,
        context: MemoryActorContext,
        include_private: bool = False,
    ) -> bool:
        if not self.visible(record, context=context, include_private=include_private):
            return False
        meta = record.metadata or {}
        if str(meta.get("scope") or "") == "device":
            source_device = str(meta.get("source_device_id") or "")
            target_device = str(meta.get("target_device_id") or "")
            return bool(context.device_id) and context.device_id in {
                source_device,
                target_device,
            }
        return True

    def score(
        self,
        record: MemoryWireRecord,
        *,
        context: MemoryActorContext,
        query: str,
    ) -> float:
        meta = record.metadata or {}
        scope = str(meta.get("scope") or "persona")
        source_device = str(meta.get("source_device_id") or "")
        target_device = str(meta.get("target_device_id") or "")
        session_id = str(meta.get("session_id") or "")
        if context.session_id and session_id and session_id == context.session_id:
            score = 1.0
        elif (
            scope == "device"
            and bool(context.device_id)
            and context.device_id in {source_device, target_device}
        ):
            score = 0.9
        elif scope in {"global", "persona"}:
            score = 0.75
        elif scope == "agent":
            score = 0.7
        elif scope == "device":
            score = 0.25
        else:
            score = 0.5
        if meta.get("source") == "user-confirmed":
            score += 0.2
        extensions = parse_extensions(meta)
        for namespace, payload in extensions.items():
            del payload
            policy = self._extension_policies.get(namespace)
            if policy is not None:
                score += policy.boost(record, context=context, query=query)
        return score

    def rank(
        self,
        records: list[MemoryWireRecord],
        *,
        context: MemoryActorContext,
        query: str,
        top_k: int,
        include_private: bool = False,
    ) -> list[MemoryWireRecord]:
        visible = [
            r for r in records
            if self.visible(r, context=context, include_private=include_private)
        ]
        visible.sort(
            key=lambda r: (
                (r.metadata or {}).get("source") == "user-confirmed",
                self.score(r, context=context, query=query),
                float((r.metadata or {}).get("similarity") or 0.0),
            ),
            reverse=True,
        )
        return visible[:top_k]
