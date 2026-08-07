"""Does a write get slower as the graph grows? It is on the turn path, so it matters more than a read.

The scale probe's write column is flat, but it measures a triple whose subject
already exists. The fill loop creates a new object entity every time and appeared
— by wall-clock arithmetic, which is not a measurement — to slow down. This asks
directly, on the graph the wall probe left behind.
"""

from __future__ import annotations

import asyncio, os, sys, time

NONCE = str(time.time_ns())  # every run writes keys nothing has seen, or it measures a no-op
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.domain.space_lock import SpaceLock


def p(values, q):
    o = sorted(values)
    return round(o[max(0, int(len(o) * q) - 1)], 3)


async def bench(kg, label, rounds=200):
    hot, fresh = [], []
    for r in range(rounds):
        t = time.perf_counter()
        await kg.add_triple(subject="用户", predicate="likes", object=f"热{NONCE}{label}{r}",
                            audience="owner", source_turn_id=f"hot-{NONCE}-{label}-{r}")
        hot.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        await kg.add_triple(subject=f"新人{NONCE}{label}{r}", predicate="lives_in", object=f"新地{NONCE}{label}{r}",
                            audience="owner", source_turn_id=f"new-{NONCE}-{label}-{r}")
        fresh.append((time.perf_counter() - t) * 1000)
    stats = await kg.stats()
    print(f"{label:>10} │ {stats['triples_total']:>7} stmts {stats['entities']:>7} ents │ "
          f"existing subject {p(hot,0.5):>6.2f} p95 {p(hot,0.95):>6.2f} │ "
          f"two new entities {p(fresh,0.5):>6.2f} p95 {p(fresh,0.95):>6.2f}", flush=True)


async def main():
    root = Path(os.environ.get("WALL_DIR", "/tmp"))
    big = root / "kg-wall" / "kg.sqlite3"
    small = root / "kg-small" / "kg.sqlite3"
    small.parent.mkdir(parents=True, exist_ok=True)
    if small.exists():
        small.unlink()

    kg_small = SqliteKnowledgeGraph(small, space_id="default.wall.default", lock=SpaceLock())
    await bench(kg_small, "empty")
    kg_small.close()

    if big.exists():
        kg_big = SqliteKnowledgeGraph(big, space_id="default.wall.default", lock=SpaceLock())
        await bench(kg_big, "180k-ents")
        kg_big.close()
    else:
        print("no large graph found at", big)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
