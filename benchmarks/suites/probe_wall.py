"""Where the graph stops fitting the voice budget on the real board.

The scale probe stops at 60k because that is a year of heavy use. This one keeps
going until entity matching crosses the 50 ms the recall path gives the graph
(``memory_settings.py``), because that crossing is the only thing that decides
whether forgetting is urgent, optional, or unnecessary — and until now it has
only ever been extrapolated, twice, from a laptop.

Reported against *entities*, not statements: matching scans ``kg_entities``, and a
statement purge was measured to move it by nothing at all.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.domain.space_lock import SpaceLock

OWNER = "owner"
SPACE = "default.wall.default"
BUDGET_MS = 50.0

_PEOPLE = ["妈妈", "爸爸", "张丽", "老王", "铁锤", "小美", "李医生", "房东"]
_PLACES = ["杭州", "北京", "西湖区", "公司", "健身房", "老家"]
_VERBS = ["likes", "works_at", "lives_in", "owns", "promised", "friend_of"]
_QUERIES = ["我妈妈住在哪里", "我在哪工作", "铁锤是谁", "我答应过什么", "我朋友有谁"]


def _p(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, int(len(ordered) * q) - 1)], 3)


async def fill(kg: SqliteKnowledgeGraph, count: int, start: int) -> None:
    entities_wanted = max(1, (start + count) // 8)
    for i in range(count):
        n = start + i
        subject = "用户" if i % 3 else f"{_PEOPLE[n % len(_PEOPLE)]}{n % entities_wanted}"
        await kg.add_triple(
            subject=subject,
            predicate=_VERBS[n % len(_VERBS)],
            object=f"{_PLACES[n % len(_PLACES)]}{n}",
            audience=OWNER,
            source_turn_id=f"turn-{n}",
        )


async def measure(kg: SqliteKnowledgeGraph, rounds: int = 20) -> tuple[float, float, float]:
    """match, combined, and what recall actually pays: the two together."""

    match_ms, combined_ms, total_ms = [], [], []
    for r in range(rounds):
        query = f"{_QUERIES[r % len(_QUERIES)]}{r}"
        started = time.perf_counter()
        names = await kg.match_entities_for_query(query, cap=3)
        after_match = time.perf_counter()
        await kg.query_entity_combined(names or ["用户"], audiences=(OWNER,), limit_per_entity=8)
        finished = time.perf_counter()
        match_ms.append((after_match - started) * 1000)
        combined_ms.append((finished - after_match) * 1000)
        total_ms.append((finished - started) * 1000)
    return _p(match_ms, 0.5), _p(combined_ms, 0.5), _p(total_ms, 0.95)


async def main() -> int:
    root = Path(os.environ.get("WALL_DIR", "/tmp")) / "kg-wall"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "kg.sqlite3"
    if path.exists():
        path.unlink()
    kg = SqliteKnowledgeGraph(path, space_id=SPACE, lock=SpaceLock())

    print(f"{'statements':>10} {'entities':>9} {'MB':>6} │ "
          f"{'match':>7} {'combined':>9} {'graph p95':>10} │ budget")
    print("─" * 74)

    written = 0
    for target in (20_000, 60_000, 100_000, 150_000, 200_000, 300_000):
        await fill(kg, target - written, written)
        written = target
        stats = await kg.stats()
        match, combined, p95 = await measure(kg)
        size = os.path.getsize(path) / (1024 * 1024)
        verdict = "fits" if p95 <= BUDGET_MS else "BLOWS"
        print(
            f"{stats['triples_total']:>10} {stats['entities']:>9} {size:>6.1f} │ "
            f"{match:>7.2f} {combined:>9.2f} {p95:>10.2f} │ {verdict}",
            flush=True,
        )
        if p95 > BUDGET_MS:
            print(f"\nCrossed {BUDGET_MS:.0f} ms at {stats['entities']} entities.")
            break

    kg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
