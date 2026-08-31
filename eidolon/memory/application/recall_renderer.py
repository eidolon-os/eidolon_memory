"""Render durable recall projections into the ``[MEMORY]`` block fed to the LLM.

Conversation history belongs to Agent context assembly.  This renderer only
combines Memory-owned projections: graph statements, themes and vector records.
"""

from __future__ import annotations

from datetime import UTC
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

# Phase 4 — high-level themes from the consolidator. Capped at 4 lines so
# the [主题] section doesn't crowd out vector recall on busy users.
_THEMES_MAX_ITEMS = 4


def _truncate(text: str, limit: int = 200) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def _time_prefix(rec: MemoryWireRecord) -> str:
    if rec.memory_time is None:
        return ""
    dt = rec.memory_time
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return f"[{dt.strftime('%Y-%m-%d')}] "


def _is_theme_record(rec: MemoryWireRecord) -> bool:
    """Phase 4 — themes come from the consolidator and live in Wing_Theme.

    Either signal qualifies: the wing tag (preferred — set at ingest time)
    or the source marker (defensive — survives wing-config edits). One ⇒
    the record renders in the [主题] section instead of the vector groups.
    """
    meta = rec.metadata or {}
    return meta.get("wing") == "Wing_Theme" or meta.get("source") == "consolidator"


def _render_themes(themes: list[MemoryWireRecord]) -> list[str]:
    """Render the ``[主题]`` section (cross-time consolidator summaries)."""
    if not themes:
        return []
    lines: list[str] = ["[主题]"]
    for rec in themes[:_THEMES_MAX_ITEMS]:
        underlying = (rec.metadata or {}).get("underlying_wing")
        text = _time_prefix(rec) + _truncate(str(rec.value or ""))
        if underlying:
            lines.append(f"- ({underlying}) {text}")
        else:
            lines.append(f"- {text}")
    return lines


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

    Section order (top → bottom):
      1. ``知识图谱事实`` — KG triples (high-confidence structured facts)
      2. ``[主题]`` — consolidator-distilled cross-time themes
      3. Vector fragments grouped by ``metadata.memory_type``

    Each section is independently skipped if its source is empty. Vector
    sections are individually capped at ``_MAX_ITEMS_PER_GROUP``; themes at
    ``_THEMES_MAX_ITEMS``; KG transcription handles its own capping (caller
    supplies the trimmed triple list).
    """
    lines: list[str] = []

    if kg_triples:
        if lines:
            lines.append("")
        lines.append(transcribe_triples(kg_triples))

    # Phase 4 — split themes out of the raw record list so they render in
    # their own [主题] section instead of leaking into a vector group whose
    # ``memory_type`` taxonomy was never designed for cross-time summaries.
    theme_records: list[MemoryWireRecord] = []
    vector_records: list[MemoryWireRecord] = []
    for rec in records:
        if _is_theme_record(rec):
            theme_records.append(rec)
        else:
            vector_records.append(rec)

    theme_lines = _render_themes(theme_records)
    if theme_lines:
        if lines:
            lines.append("")
        lines.extend(theme_lines)

    # Vector fragments — grouped + capped per group.
    groups: dict[str, list[str]] = {title: [] for title in _WING_GROUP_MAP}
    for rec in vector_records:
        kind = str(rec.metadata.get("memory_type", ""))
        text = _time_prefix(rec) + str(rec.value)
        groups[_classify(kind)].append(text)

    vector_lines: list[str] = []
    for title, items in groups.items():
        if items:
            vector_lines.append(f"{title}:")
            vector_lines.extend(f"- {item}" for item in items[:_MAX_ITEMS_PER_GROUP])
    if vector_lines:
        if lines:
            lines.append("")
        lines.extend(vector_lines)

    return "\n".join(lines)
