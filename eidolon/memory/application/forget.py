"""Privacy-safe candidate resolution and verified drawer deletion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from eidolon.memory.domain.wire import MemoryWireRecord

_COMMAND_RE = re.compile(
    r"(?:请|麻烦)?(?:帮我|把|给我)?(?:忘掉|删掉|删除|抹掉|不要记住|别记住|别记录)"
)
_SUFFIX_RE = re.compile(r"(?:这条|这段|相关的|有关的)?(?:的)?(?:记忆|内容|信息|事情|事实)$")
_NON_SEMANTIC_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_GENERIC_TARGETS = frozenset({"", "刚才", "这件事", "那件事", "这个", "那个", "全部"})


@dataclass(frozen=True, slots=True)
class ForgetCandidate:
    key: str
    text: str
    wing: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "drawer_id": self.key,
            "text": self.text,
            "wing": self.wing,
            "score": self.score,
        }


def extract_privacy_target(text: str) -> str:
    """Remove command boilerplate while preserving the fact/topic itself."""
    clean = (text or "").strip().strip("，。！？,.!? ")
    clean = re.sub(r"^(?:请|麻烦)(?:帮我)?", "", clean).strip()
    clean = _COMMAND_RE.sub("", clean, count=1).strip("，。！？,.!? ")
    clean = _SUFFIX_RE.sub("", clean).strip("，。！？,.!? ")
    return clean


def _normalize(text: Any) -> str:
    return _NON_SEMANTIC_RE.sub("", str(text or "").lower())


def _belongs_to_space(record: MemoryWireRecord, memory_space_id: str) -> bool:
    recorded_space = str(record.metadata.get("memory_space_id") or record.memory_space_id)
    return recorded_space == memory_space_id


async def find_forget_candidates(
    backend: Any,
    memory_space_id: str,
    target: str,
    *,
    max_scan: int = 5000,
    max_candidates: int = 20,
) -> list[ForgetCandidate]:
    """Return only high-confidence, tenant-scoped matches for confirmation."""
    clean_target = extract_privacy_target(target)
    normalized_target = _normalize(clean_target)
    if clean_target in _GENERIC_TARGETS or len(normalized_target) < 2:
        return []

    # Stable IDs are already unambiguous; avoid an O(n) palace listing for
    # exact-delete flows and large realms.
    if clean_target.startswith("drawer_"):
        record = await backend.get(memory_space_id, clean_target)
        if record is None or not _belongs_to_space(record, memory_space_id):
            return []
        return [
            ForgetCandidate(
                key=record.key,
                text=str(record.value or ""),
                wing=str(record.metadata.get("wing") or ""),
                score=1.0,
            )
        ]

    rows = await backend.get_all(
        memory_space_id,
        limit=max(1, min(max_scan, 50_000)),
        offset=0,
    )
    candidates: list[ForgetCandidate] = []
    for record in rows:
        if not _belongs_to_space(record, memory_space_id):
            continue
        normalized_key = _normalize(record.key)
        normalized_text = _normalize(record.value)
        memory_id = _normalize(record.metadata.get("memory_id"))
        if normalized_target in {normalized_key, memory_id}:
            score = 1.0
        elif normalized_target == normalized_text:
            score = 1.0
        elif normalized_target in normalized_text:
            score = 0.9
        elif len(normalized_text) >= 4 and normalized_text in normalized_target:
            score = 0.85
        else:
            continue
        candidates.append(
            ForgetCandidate(
                key=record.key,
                text=str(record.value or ""),
                wing=str(record.metadata.get("wing") or ""),
                score=score,
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.key))
    return candidates[: max(1, min(max_candidates, 100))]


async def delete_exact_drawers(
    backend: Any,
    memory_space_id: str,
    drawer_ids: list[str],
) -> list[str]:
    """Idempotently delete confirmed IDs and prove each is no longer visible."""
    unique_ids = list(dict.fromkeys(key.strip() for key in drawer_ids if key.strip()))
    if not unique_ids:
        raise ValueError("at least one drawer_id is required")

    deleted: list[str] = []
    for key in unique_ids:
        if not key.startswith("drawer_"):
            raise ValueError(f"invalid MemPalace drawer_id: {key}")
        existing = await backend.get(memory_space_id, key)
        if existing is not None and not _belongs_to_space(existing, memory_space_id):
            raise PermissionError(f"drawer does not belong to memory space: {key}")
        if existing is not None:
            await backend.delete(memory_space_id, key)
        if await backend.get(memory_space_id, key) is not None:
            raise RuntimeError(f"drawer remains visible after delete: {key}")
        deleted.append(key)
    return deleted
