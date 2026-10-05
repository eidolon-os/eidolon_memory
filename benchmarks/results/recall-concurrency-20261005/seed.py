"""Isolated synthetic corpus; real bge-small-zh, Chroma and SQLite, no LLM."""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter  # noqa: E402
from eidolon.memory.config.memory_settings import MemorySettings  # noqa: E402

RUN = Path("/private/tmp/eidolon-recall-20261005b")
SETTINGS = RUN / "settings.yaml"
FACTS = [
    ("Wing_Life", "用户最近的情绪状态", "用户最近心情愉快，周末散步后放松了。"),
    ("Wing_Profile", "用户喜欢什么音乐", "用户喜欢钢琴音乐，每晚听古典乐。"),
    ("Wing_Work", "工作压力", "用户最近工作压力很大，项目周五截止。"),
    ("Wing_Life", "和家人的关系", "用户和妈妈关系很好，每周一起吃饭。"),
    ("Wing_Life", "最近的健康状况", "用户最近睡眠不足，计划早点休息。"),
    ("Wing_Profile", "兴趣爱好", "用户的兴趣爱好是徒步和摄影。"),
    ("Wing_Life", "重要的事件", "用户上周参加了朋友的婚礼。"),
    ("Wing_Profile", "生活习惯和偏好", "用户每天早上喝乌龙茶，不喝咖啡。"),
]


async def main():
    if SETTINGS.exists():
        raise RuntimeError("Refusing to reseed an existing benchmark")
    RUN.mkdir(parents=True, exist_ok=True)
    settings = MemorySettings.model_validate(
        {
            "runtime": {"palaces_root": str(RUN / "palaces"), "run_dir": str(RUN / "run")},
            "embedding": {
                "provider": "http",
                "model": "bge-small-zh",
                "http": {
                    "base_url": "http://127.0.0.1:18783/v1",
                    "model": "bge-small-zh",
                    "dimension": 512,
                },
            },
        }
    )
    SETTINGS.write_text(settings.model_dump_json(indent=2))
    os.environ["MEMPALACE_HOME"] = str(RUN / "home")
    os.environ["EIDOLON_MEMORY_RUN_DIR"] = str(RUN / "run")
    router = LocalPalaceRouter(settings, allowed_spaces=["recall-probe"])
    try:
        runtime = await router.resolve("recall-probe")
        for n in range(16):
            for i, (wing, subject, text) in enumerate(FACTS):
                await runtime.backend.ingest_text(
                    wing=wing,
                    room=f"fact-{n}-{i}",
                    text=f"{text} 记录编号 {n}。",
                    metadata={
                        "memory_space_id": runtime.space_id,
                        "audience": "companion:benchmark",
                        "scope": "persona",
                        "visibility": "all_devices",
                        "source_turn_id": f"turn-{n}-{i}",
                    },
                )
        for i, (_, subject, text) in enumerate(FACTS):
            await runtime.backend.ingest_text(
                wing="Wing_Theme",
                room=f"theme-{i}",
                text=f"{subject}：{text}",
                metadata={
                    "memory_space_id": runtime.space_id,
                    "audience": "companion:benchmark",
                    "scope": "persona",
                    "visibility": "all_devices",
                    "memory_type": "theme",
                },
            )
            await runtime.kg.add_triple(
                subject=subject,
                predicate="has_context",
                object=text,
                audience="companion:benchmark",
                source_turn_id=f"turn-0-{i}",
            )
        print("Seeded 128 facts, 8 themes, 8 triples:", runtime.palace_path)
    finally:
        await router.aclose()


asyncio.run(main())
