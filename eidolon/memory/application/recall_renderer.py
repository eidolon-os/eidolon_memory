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
    from eidolon.memory.domain.payloads import ConversationTurnPayload
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

# Phase 2 — verbatim recent-turn section. Capped so very chatty users don't
# blow the LLM context budget. ``_WM_TRUNC`` keeps any individual user/assistant
# utterance bounded — a user pasting a diary entry shouldn't dominate.
_WM_MAX_TURNS = 5
_WM_TRUNC = 200

# Phase 4 — high-level themes from the consolidator. Capped at 4 lines so
# the [主题] section doesn't crowd out vector recall on busy users.
_THEMES_MAX_ITEMS = 4


def _truncate(text: str, limit: int = _WM_TRUNC) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def _render_working_memory(turns: list[ConversationTurnPayload]) -> list[str]:
    """Render the ``[最近对话]`` section (returns 0 lines when empty)."""
    if not turns:
        return []
    lines: list[str] = ["[最近对话]"]
    # Take the most recent N turns, oldest-first inside the rendered block so
    # the LLM reads them in conversation order.
    recent = turns[-_WM_MAX_TURNS:]
    for turn in recent:
        u = _truncate(turn.user_text)
        a = _truncate(turn.assistant_text)
        if u:
            lines.append(f"- 用户: {u}")
        if a:
            lines.append(f"- 你:   {a}")
    return lines


def _is_theme_record(rec: MemoryWireRecord) -> bool:
    """Phase 4 — themes come from the consolidator and live in Wing_Theme.

    Either signal qualifies: the wing tag (preferred — set at ingest time)
    or the source marker (defensive — survives wing-config edits). One ⇒
    the record renders in the [主题] section instead of the vector groups.
    """
    meta = rec.metadata or {}
    return (
        meta.get("wing") == "Wing_Theme"
        or meta.get("source") == "consolidator"
    )


def _render_themes(themes: list[MemoryWireRecord]) -> list[str]:
    """Render the ``[主题]`` section (cross-time consolidator summaries)."""
    if not themes:
        return []
    lines: list[str] = ["[主题]"]
    for rec in themes[:_THEMES_MAX_ITEMS]:
        underlying = (rec.metadata or {}).get("underlying_wing")
        text = _truncate(str(rec.value or ""))
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
    working_memory: list[ConversationTurnPayload] | None = None,
) -> str:
    """Compose the LLM-facing ``[MEMORY]`` block.

    Section order (top → bottom):
      1. ``[最近对话]`` — Phase 2 verbatim recent turns (highest priority)
      2. ``[主题]`` — Phase 4 consolidator-distilled cross-time themes
      3. Vector fragments grouped by ``metadata.memory_type``
      4. ``知识图谱事实`` — KG triples

    Each section is independently skipped if its source is empty. Vector
    sections are individually capped at ``_MAX_ITEMS_PER_GROUP``; working
    memory at ``_WM_MAX_TURNS`` × 2 lines; themes at ``_THEMES_MAX_ITEMS``;
    KG transcription handles its own capping (caller supplies the trimmed
    triple list).
    """
    lines: list[str] = []

    # Phase 2 — recent turns lead the block. They give the LLM continuity
    # before any retrieved knowledge, matching how a human would re-read
    # the last few messages before answering "what were we just talking about".
    wm_lines = _render_working_memory(working_memory or [])
    if wm_lines:
        lines.extend(wm_lines)

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
        text = str(rec.value)
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

    if kg_triples:
        if lines:
            lines.append("")
        lines.append(transcribe_triples(kg_triples))

    return "\n".join(lines)
