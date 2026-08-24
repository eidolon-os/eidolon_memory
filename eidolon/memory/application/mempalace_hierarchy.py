"""Build MemPalace wing→room→drawer hierarchy snapshots (shared by Admin façade and MCP)."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from eidolon.memory.config.memory_settings import MemorySettings, WingDefinition
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord


def mempalace_layer_infos() -> list[dict[str, str]]:
    return [
        {
            "level": "palace",
            "title": "宫殿 Palace",
            "description": (
                "单个用户、家庭或本机实例的记忆根；对应运行时解析出的磁盘目录与底层向量集合。"
            ),
        },
        {
            "level": "wing",
            "title": "翼 Wing",
            "description": (
                "顶层记忆领域（如个人画像、工作、隐私等）；与同目录 memory YAML 中 wings 条目对应。"
            ),
        },
        {
            "level": "room",
            "title": "阁 Room",
            "description": (
                "某一翼下面的稳定主题或人物线索（例如 ``profile_core``、``person_<name>``）。"
            ),
        },
        {
            "level": "drawer",
            "title": "抽屉 Drawer",
            "description": "一条可语义检索的记忆片段；列表中的 key 常为 `drawer_*` id。",
        },
    ]


ROOM_NAMING_HINTS = [
    "`profile_core`：用户核心画像。",
    "`person_<别名>`、`pet_<别名>`：重要人物与宠物。",
    "`project_<名称>`：工作/学习项目。",
    "`emotion_<主题>_<yyyy_mm>`：情绪主题。",
    "`event_<短主题>`：重要事件节点。",
    "`preference_<类>`：长期偏好。",
    "`privacy_<主题>`：禁记或封存相关。",
]


def _preview_blob(value: Any, n: int = 96) -> str:
    text = value.strip() if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


async def scan_records(
    backend: MemoryBackend, *, max_records: int
) -> tuple[list[MemoryWireRecord], bool]:
    out: list[MemoryWireRecord] = []
    offset = 0
    chunk = min(max_records, 2500)
    while len(out) < max_records:
        take = max_records - len(out)
        req = min(chunk, take)
        batch = await backend.get_all("", limit=req, offset=offset)
        if not batch:
            break
        out.extend(batch)
        offset += len(batch)
        if len(batch) < req:
            break
    peek = await backend.get_all("", limit=1, offset=offset)
    capped = bool(peek)
    return out, capped


def _rollup(
    rows: list[MemoryWireRecord],
) -> dict[str, dict[str, list[tuple[str, str]]]]:
    tree: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
    seen: set[tuple[str, str, str]] = set()
    for rec in rows:
        wing_key = str(rec.metadata.get("wing") or "").strip() or "(未标注 wing)"
        room_key = str(rec.metadata.get("room") or "").strip() or "(未标注 room)"
        drawer_key = str(rec.key)
        dk = (wing_key, room_key, drawer_key)
        if dk in seen:
            continue
        seen.add(dk)
        tree[wing_key][room_key].append((drawer_key, _preview_blob(rec.value)))

    for wd in tree.values():
        for rk, lst in wd.items():
            lst.sort(key=lambda t: t[0])
    return {w: dict(rooms) for w, rooms in tree.items()}


def _bucket_wing(
    wing_id: str,
    *,
    cfg: WingDefinition | None,
    rooms_blob: dict[str, list[tuple[str, str]]],
    max_drawers_preview: int,
) -> dict[str, Any]:
    rooms_out: list[dict[str, Any]] = []
    drawer_total = 0
    for room_id in sorted(rooms_blob.keys()):
        pairs = rooms_blob[room_id]
        n = len(pairs)
        drawer_total += n
        previews = pairs[:max_drawers_preview]
        rooms_out.append(
            {
                "room_id": room_id,
                "drawer_count": n,
                "drawers_preview": [{"key": k, "preview": p} for k, p in previews],
                "preview_truncated": n > len(previews),
            }
        )
    return {
        "wing_id": wing_id,
        "is_configured": cfg is not None,
        "display_name": cfg.display_name if cfg else "",
        "description": cfg.description if cfg else "",
        "sort_order": cfg.sort_order if cfg else 999_999,
        "room_count": len(rooms_out),
        "drawer_count": drawer_total,
        "rooms": rooms_out,
    }


async def build_owner_browse(
    backend: MemoryBackend,
    settings: MemorySettings,
    *,
    visible: Callable[[MemoryWireRecord], bool],
    max_records: int,
    max_titles_per_room: int,
) -> dict[str, Any]:
    """The palace as its Owner may see it: wings, rooms, and what is in them.

    Shares the roll-up below rather than repeating it — how records become
    wings and rooms is one question and this module owns it. What differs is the
    audience, and that arrives as ``visible``:

    - the operator snapshot passes nothing and sees the whole palace, which is
      right for someone debugging it;
    - this one passes the same policy recall uses, so a person can never be
      shown something their Eidolon could not have recalled. That includes the
      privacy wing and, once anything is marked companion-private, another
      Companion's statements.

    The predicate is injected rather than imported so this module keeps knowing
    nothing about recall policy; the dependency points inward, not sideways.

    Withheld records are *counted*, not hidden. "There are 3 things here you
    asked me not to bring up" is true and useful; a total that quietly differs
    from the number of things listed is neither.
    """

    scanned, capped = await scan_records(backend, max_records=max_records)
    allowed = [record for record in scanned if visible(record)]
    tree = _rollup(allowed)
    remaining = {
        wing: {room: list(pairs) for room, pairs in rooms.items()}
        for wing, rooms in tree.items()
    }

    wings: list[dict[str, Any]] = []
    for definition in sorted(settings.wings, key=lambda w: (w.sort_order, w.id)):
        rooms_blob = remaining.pop(definition.id, {})
        if not rooms_blob:
            # A wing with nothing in it is not shown. An Owner reading nine
            # empty categories learns nothing about what their Eidolon
            # remembers, and the empty ones are an implementation detail of the
            # fixed wing schema.
            continue
        wings.append(
            _bucket_wing(
                definition.id,
                cfg=definition,
                rooms_blob=rooms_blob,
                max_drawers_preview=max_titles_per_room,
            )
        )
    wings.extend(
        _bucket_wing(
            wing_id,
            cfg=None,
            rooms_blob=rooms_blob,
            max_drawers_preview=max_titles_per_room,
        )
        for wing_id, rooms_blob in sorted(remaining.items(), key=lambda item: item[0])
    )

    return {
        "wings": wings,
        "entry_count": len(allowed),
        #: Present and not listed. The Owner's own privacy wing is the common
        #: case, so this is a fact about their memory rather than a refusal.
        "withheld_count": len(scanned) - len(allowed),
        #: The scan is bounded, so "this is everything" is a claim this read
        #: cannot always make. Saying so beats a total that is quietly partial.
        "truncated": capped,
    }


async def build_mempalace_hierarchy_snapshot(
    backend: MemoryBackend,
    settings: MemorySettings,
    *,
    palace_path: str,
    max_records: int,
    max_drawers_per_room: int,
) -> dict[str, Any]:
    palace_path_str = palace_path
    rows, capped = await scan_records(backend, max_records=max_records)
    raw_tree = _rollup(rows)
    remaining: dict[str, dict[str, list[tuple[str, str]]]] = {
        k: {rk: list(pairs) for rk, pairs in v.items()}
        for k, v in raw_tree.items()
    }

    configured_buckets: list[dict[str, Any]] = []
    sorted_defs = sorted(settings.wings, key=lambda w: (w.sort_order, w.id))
    for wdef in sorted_defs:
        rooms_blob = remaining.pop(wdef.id, {})
        configured_buckets.append(
            _bucket_wing(
                wdef.id,
                cfg=wdef,
                rooms_blob=rooms_blob,
                max_drawers_preview=max_drawers_per_room,
            )
        )

    extra_buckets = [
        _bucket_wing(
            wid,
            cfg=None,
            rooms_blob=rmap,
            max_drawers_preview=max_drawers_per_room,
        )
        for wid, rmap in sorted(remaining.items(), key=lambda x: x[0])
    ]

    return {
        "palace_path": palace_path_str,
        "layers": mempalace_layer_infos(),
        "room_naming_conventions": list(ROOM_NAMING_HINTS),
        "steward_mode": settings.steward.mode,
        "total_records_scanned": len(rows),
        "capped_by_max_records": capped,
        "configured_wings": configured_buckets,
        "extra_wings": extra_buckets,
    }
