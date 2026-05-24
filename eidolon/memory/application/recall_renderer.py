"""Render recall results into the ``[MEMORY]`` block fed to LLM.

Separate from `public_recall.py` because future phases extend this layer
independently of the fusion / routing logic:

- Phase 2 (working memory) adds a ``[最近对话]`` section at the very top
- Phase 4 (consolidator) adds a ``[主题]`` section after working memory
- Vector fragments and KG triples are unchanged by either

Keeping the renderer in its own module means the recall pipeline doesn't
grow a new branch every time we add a context section — each phase adds an
input parameter to `group_recall_context` and a single guarded block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eidolon.memory.application.kg_recall import transcribe_triples

if TYPE_CHECKING:
    from eidolon.memory.domain.wire import MemoryWireRecord


# Section title → metadata.memory_type values that route into it. Ordered so
# the rendered output is stable (LLM context order matters).
_WING_GROUP_MAP: dict[str, frozenset[str]] = {
    "个人画像与健康": frozenset({"profile", "health"}),
    "人机互动": frozenset({"interaction"}),
    "关系": frozenset({"relationship"}),
    "情绪": frozenset({"emotion"}),
    "愿景与目标": frozenset({"goal"}),
    "工作学习": frozenset({"work"}),
    "生活方式与近况": frozenset({"preference", "life"}),
}
_DEFAULT_GROUP = "生活方式与近况"
_MAX_ITEMS_PER_GROUP = 4


def _classify(memory_type: str) -> str:
    """Return the section title for a given memory_type, defaulting to lifestyle."""
    mt = (memory_type or "").lower()
    for title, members in _WING_GROUP_MAP.items():
        if mt in members:
            return title
    return _DEFAULT_GROUP


def group_recall_context(
    records: list[MemoryWireRecord],
    *,
    kg_triples: list | None = None,
) -> str:
    """Compose the LLM-facing ``[MEMORY]`` block.

    Vector fragments are grouped by ``metadata.memory_type`` into 7 ordered
    sections (each capped at 4 items so LLM context stays bounded). KG
    triples, if any, appear as a separate ``知识图谱事实`` section.
    """
    groups: dict[str, list[str]] = {title: [] for title in _WING_GROUP_MAP}
    for rec in records:
        kind = str(rec.metadata.get("memory_type", ""))
        text = str(rec.value)
        groups[_classify(kind)].append(text)

    lines: list[str] = []
    for title, items in groups.items():
        if items:
            lines.append(f"{title}:")
            lines.extend(f"- {item}" for item in items[:_MAX_ITEMS_PER_GROUP])

    if kg_triples:
        if lines:
            lines.append("")
        lines.append(transcribe_triples(kg_triples))

    return "\n".join(lines)
