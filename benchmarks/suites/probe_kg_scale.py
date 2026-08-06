"""How the graph behaves as a real companion fills it, not at 200 statements.

    uv run python benchmarks/suites/probe_kg_scale.py

Rate, from the e2e suite: 1–3 triples per turn. An engaged user is 50–200 turns a
day, so 100–500 statements a day, 36k–180k a year. Entities grow sub-linearly —
people re-mention the same subjects — at roughly one entity per eight statements.
Nothing here needs NATS, an LLM or an embedder; it drives the graph directly, so it
runs on a board during bring-up.

**Complexity is what transfers, constants are not.** The milliseconds below are a
12-core laptop's; a Pi 5 is roughly 3–5x slower per core. A read that doubles when
the graph doubles will keep doubling there, from a slower start — which is the
thing worth knowing before the board has a year of conversation on it.

The shape matters because recall gives the graph a **50 ms budget on the voice
path** and silently degrades to vector-only when it is missed. A graph that has
quietly stopped contributing looks like a companion that has stopped noticing
relationships, and nothing logs it.

**The hot subject is the point of the fixture, not an artefact.** Two thirds of the
statements here have "用户" as their subject, because in a companion's graph most
facts are about the user. Every linear read this probe found was linear *because*
of that shape, and a fixture with evenly-spread subjects would have shown none of
them.

Measured 2026-08-06 on an Apple M3 Pro, before and after bounding the reads
(``96c9bef``..): the four reads went from 79/127/192/203 ms at 60k statements to
28/0.2/1.0/0.6. Only ``match`` is still linear — its test asks whether a stored
name occurs *in the query*, which is the direction an index cannot serve.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.domain.space_lock import SpaceLock

OWNER = "owner"
SPACE = "default.scale.default"

_PEOPLE = ["妈妈", "爸爸", "张丽", "老王", "铁锤", "小美", "李医生", "房东"]
_PLACES = ["杭州", "北京", "西湖区", "公司", "健身房", "老家"]
_VERBS = ["likes", "works_at", "lives_in", "owns", "promised", "friend_of"]


def _p(values: list[float], q: float) -> float:
    o = sorted(values)
    return round(o[max(0, int(len(o) * q) - 1)], 3)


async def fill(kg: SqliteKnowledgeGraph, statements: int, start: int) -> None:
    """Write ``statements`` triples with a realistic entity:statement ratio."""

    entities_wanted = max(1, statements // 8)
    for i in range(statements):
        n = start + i
        # A long tail of distinct subjects, plus a hot few — which is what a real
        # graph looks like: "用户" is in most statements, everyone else is sparse.
        subject = "用户" if i % 3 else f"{_PEOPLE[n % len(_PEOPLE)]}{n % entities_wanted}"
        await kg.add_triple(
            subject=subject,
            predicate=_VERBS[n % len(_VERBS)],
            object=f"{_PLACES[n % len(_PLACES)]}{n}",
            audience=OWNER,
            source_turn_id=f"turn-{n}",
        )
        if i % 8 == 0:
            await kg.record_entity_mention(
                entity_id=subject.strip().lower(), alias=f"别名{n}", source="steward"
            )


async def measure(kg: SqliteKnowledgeGraph, rounds: int = 25) -> dict:
    queries = [
        "我妈妈住在哪里", "我在哪工作", "铁锤是谁", "我答应过什么", "我朋友有谁",
    ]

    match_ms, subjects_ms, combined_ms, entity_ms, write_ms = [], [], [], [], []
    for r in range(rounds):
        q = f"{queries[r % len(queries)]}{r}"

        t = time.perf_counter()
        names = await kg.match_entities_for_query(q, cap=3)
        match_ms.append((time.perf_counter() - t) * 1000)

        probe = names or ["用户"]
        t = time.perf_counter()
        await kg.query_subjects(probe, audiences=(OWNER,), limit_per_subject=8)
        subjects_ms.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        await kg.query_entity_combined(probe, audiences=(OWNER,), limit_per_entity=8)
        combined_ms.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        await kg.query_entity("用户", audiences=(OWNER,), direction="outgoing")
        entity_ms.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        await kg.add_triple(
            subject="用户", predicate="likes", object=f"新事物{r}",
            audience=OWNER, source_turn_id=f"probe-{r}",
        )
        write_ms.append((time.perf_counter() - t) * 1000)

    return {
        "match_p50": _p(match_ms, 0.5), "match_p95": _p(match_ms, 0.95),
        "subjects_p50": _p(subjects_ms, 0.5),
        "combined_p50": _p(combined_ms, 0.5),
        "entity_p50": _p(entity_ms, 0.5), "entity_p95": _p(entity_ms, 0.95),
        "write_p50": _p(write_ms, 0.5),
    }


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="kg-scale-"))
    path = root / "kg.sqlite3"
    kg = SqliteKnowledgeGraph(path, space_id=SPACE, lock=SpaceLock())

    print(f"{'statements':>10} {'entities':>9} {'MB':>6} │ "
          f"{'match p50':>9} {'p95':>7} │ {'subjects':>8} {'combined':>8} "
          f"{'entity p50':>10} {'p95':>7} │ {'write':>6}")
    print("─" * 108)

    written = 0
    for target in (1_000, 5_000, 20_000, 60_000):
        await fill(kg, target - written, written)
        written = target
        stats = await kg.stats()
        m = await measure(kg)
        size = os.path.getsize(path) / (1024 * 1024)
        print(
            f"{stats['triples_total']:>10} {stats['entities']:>9} {size:>6.1f} │ "
            f"{m['match_p50']:>9.2f} {m['match_p95']:>7.2f} │ "
            f"{m['subjects_p50']:>8.2f} {m['combined_p50']:>8.2f} "
            f"{m['entity_p50']:>10.2f} {m['entity_p95']:>7.2f} │ {m['write_p50']:>6.2f}"
        )

    kg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
