"""MemPalace 四层架构视图：YAML 配置的翼 + 存储中观测到的 Wing→Room→Drawer 树."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from dependencies import AdminAuth, BackendDep, SettingsDep
from fastapi import APIRouter, Query
from schemas import (
    HierarchyDrawerPreview,
    HierarchyRoomOut,
    HierarchyWingOut,
    MemPalaceHierarchyResponse,
    MemPalaceLayerInfo,
)

from eidolon.memory.config.memory_settings import WingDefinition
from eidolon.memory.config.palace_directory import resolve_palace_directory

router = APIRouter(prefix="/hierarchy", tags=["hierarchy"])


_MEMPALACE_LAYERS: list[MemPalaceLayerInfo] = [
    MemPalaceLayerInfo(
        level="palace",
        title="宫殿 Palace",
        description="单个用户、家庭或本机实例的记忆根；对应运行时解析出的磁盘目录与底层向量集合。",
    ),
    MemPalaceLayerInfo(
        level="wing",
        title="翼 Wing",
        description=(
            "顶层记忆领域（如个人画像、工作、隐私等）；与同目录 memory YAML 中 wings 条目对应。"
        ),
    ),
    MemPalaceLayerInfo(
        level="room",
        title="阁 Room",
        description="某一翼下面的稳定主题或人物线索（例如 `profile_core`、`person_<name>`）。",
    ),
    MemPalaceLayerInfo(
        level="drawer",
        title="抽屉 Drawer",
        description="一条可语义检索的记忆片段；列表中的 key 常为 `drawer_*` id。",
    ),
]

_ROOM_HINTS = [
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


async def _scan_records(backend: Any, *, max_records: int) -> tuple[list[Any], bool]:
    out: list[Any] = []
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
    rows: list[Any],
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
) -> HierarchyWingOut:
    rooms_out: list[HierarchyRoomOut] = []
    drawer_total = 0
    for room_id in sorted(rooms_blob.keys()):
        pairs = rooms_blob[room_id]
        n = len(pairs)
        drawer_total += n
        previews = pairs[:max_drawers_preview]
        rooms_out.append(
            HierarchyRoomOut(
                room_id=room_id,
                drawer_count=n,
                drawers_preview=[
                    HierarchyDrawerPreview(key=k, preview=p) for k, p in previews
                ],
                preview_truncated=n > len(previews),
            )
        )
    return HierarchyWingOut(
        wing_id=wing_id,
        is_configured=cfg is not None,
        display_name=(cfg.display_name if cfg else ""),
        description=(cfg.description if cfg else ""),
        sort_order=cfg.sort_order if cfg else 999_999,
        room_count=len(rooms_out),
        drawer_count=drawer_total,
        rooms=rooms_out,
    )


@router.get("", response_model=MemPalaceHierarchyResponse)
@router.get("/", response_model=MemPalaceHierarchyResponse)
async def mempalace_hierarchy(
    _: AdminAuth,
    backend: BackendDep,
    settings: SettingsDep,
    max_records: int = Query(8000, ge=50, le=50_000, description="最多扫描的记录条数。"),
    max_drawers_per_room: int = Query(
        48,
        ge=4,
        le=400,
        description="每个 Room 下列出的抽屉预览条数上限；完整计数见 drawer_count。",
    ),
) -> MemPalaceHierarchyResponse:
    palace_path_str = str(resolve_palace_directory(settings))

    rows, capped = await _scan_records(backend, max_records=max_records)
    raw_tree = _rollup(rows)
    remaining: dict[str, dict[str, list[tuple[str, str]]]] = {
        k: {rk: list(pairs) for rk, pairs in v.items()}
        for k, v in raw_tree.items()
    }

    configured_buckets: list[HierarchyWingOut] = []
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

    return MemPalaceHierarchyResponse(
        palace_path=palace_path_str,
        layers=_MEMPALACE_LAYERS,
        room_naming_conventions=_ROOM_HINTS,
        steward_mode=settings.steward.mode,
        total_records_scanned=len(rows),
        capped_by_max_records=capped,
        configured_wings=configured_buckets,
        extra_wings=extra_buckets,
    )
