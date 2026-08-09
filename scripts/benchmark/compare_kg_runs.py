#!/usr/bin/env python3
"""Diff two quality runs on the dimensions a KG change is supposed to move.

``summary.md`` reports accuracy by category, which is the wrong instrument for
an extraction change: the 90-turn run on 2026-08-09 scored 31/96 with an entity
table that was 60% event phrases and held the same person under two ids, and no
per-category number said either of those things.

So this reads the graphs themselves and reports the four properties the fusion
design is trying to change, plus the per-category deltas beside them:

* **identity fragmentation** — one person under several ids, which splits their
  facts so that each half looks correct alone
* **phrase nodes** — statement objects that are occurrences rather than things
  (``送铁锤到_mother_处``), which inflate the entity space without connecting it
* **translated names** — ASCII ids in an all-Chinese corpus, the proxy for one
  thing stored under two names
* **connectivity** — how much of the graph is reachable from another entity at
  all, which is the precondition for any question needing two facts

Usage::

    uv run python scripts/benchmark/compare_kg_runs.py BEFORE_DIR AFTER_DIR

Each argument is a ``reports/memory_quality_*`` directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

#: Ids whose shape says "this is an occurrence, not a thing".
#:
#: Deliberately a *heuristic used for reporting only*. An attempt to make this
#: same judgement at the write boundary failed: `云舟科技` and `铁锤` are real
#: entities with no prefix, and `insomnia` and `boredom` are literals of exactly
#: the same shape, so form cannot separate them. It is honest as a metric —
#: where it is wrong it is wrong the same way in both runs — and dishonest as an
#: enforcement rule.
_PHRASE_HINTS = (
    re.compile(r"[，。、「」（）]"),  # punctuation only a sentence carries
    re.compile(r"^.{9,}$"),  # long enough that it is describing, not naming
    re.compile(r"_[a-z]+_"),  # snake-cased clause: 送铁锤到_mother_处
)

_ROLES = ("mother", "father", "sister", "brother", "wife", "husband", "son", "daughter")


def _graph(run: Path) -> sqlite3.Connection:
    ledgers = list(run.glob("palaces/*.ledgers"))
    if not ledgers:
        raise SystemExit(f"{run}: no .ledgers directory — not a quality run?")
    return sqlite3.connect(f"file:{ledgers[0] / 'knowledge_graph.sqlite3'}?mode=ro", uri=True)


def _entities(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT entity_id FROM kg_entities")]


def _edges(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return list(conn.execute("SELECT subject_id, predicate, object_id FROM kg_statements"))


def looks_like_phrase(entity_id: str) -> bool:
    return any(pattern.search(entity_id) for pattern in _PHRASE_HINTS)


def fragmented_roles(entities: list[str]) -> dict[str, list[str]]:
    """A role held both bare and with a name appended is one person, twice."""

    out: dict[str, list[str]] = {}
    for role in _ROLES:
        ids = sorted({e for e in entities if e == role or e.startswith(f"{role}:")})
        if len(ids) > 1:
            out[role] = ids
    return out


def connectivity(entities: list[str], edges: list[tuple[str, str, str]]) -> dict[str, Any]:
    """How many entities touch another entity, rather than only a literal.

    An entity whose every edge ends at a phrase or a state is a leaf: it can be
    looked up, and nothing can be reached from it.
    """

    # Only edges between two *things* count. A first version counted any edge
    # into kg_entities and reported 100% connectivity for a graph in which
    # nothing could be walked — because `mother -> 照看铁锤` has a phrase node on
    # the far end, and the phrase node is in kg_entities like everything else.
    real = {e for e in entities if not looks_like_phrase(e)}
    neighbours: dict[str, set[str]] = defaultdict(set)
    for subject, _predicate, obj in edges:
        if subject in real and obj in real and subject != obj:
            neighbours[subject].add(obj)
            neighbours[obj].add(subject)
    linked = {e for e in real if neighbours[e]}
    return {
        "entities": len(entities),
        "phrase_nodes": len(entities) - len(real),
        "linked_to_another_entity": len(linked),
        "linked_pct": round(100 * len(linked) / max(1, len(real)), 1),
    }


def ascii_named(entities: list[str]) -> list[str]:
    """Ids with no CJK at all, in a corpus that is entirely Chinese.

    A proxy for the model translating what it stores — 米氮平 became
    ``mirtazapine`` and 合唱团 became ``choir``, so one thing named twice is two
    entities. Counted rather than paired: proving two names mean one thing needs
    the corpus, and a count is enough to compare two runs.

    Roles (``mother``, ``self``) are excluded — those are ASCII by design.
    """

    schema = set(_ROLES) | {"self"}
    return sorted(
        e
        for e in entities
        if e.isascii() and not looks_like_phrase(e) and e.split(":")[0] not in schema
    )


def categories(run: Path) -> dict[str, dict[str, int]]:
    raw = run / "raw_results.json"
    if not raw.is_file():
        return {}
    per_query = json.loads(raw.read_text(encoding="utf-8")).get("per_query", [])
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "kg": 0, "vec": 0})
    for row in per_query:
        bucket = out[row["category"]]
        bucket["n"] += 1
        bucket["kg"] += bool(row.get("kg_hit"))
        bucket["vec"] += bool(row.get("vector_hit"))
    return dict(out)


def _delta(before: int | float, after: int | float) -> str:
    diff = after - before
    if diff == 0:
        return "  ="
    return f"{diff:+g}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args(argv)

    runs = {}
    for label, path in (("before", args.before), ("after", args.after)):
        conn = _graph(path)
        entities, edges = _entities(conn), _edges(conn)
        runs[label] = {
            "entities": entities,
            "edges": edges,
            "conn": connectivity(entities, edges),
            "roles": fragmented_roles(entities),
            "langs": ascii_named(entities),
            "cats": categories(path),
        }
        conn.close()

    b, a = runs["before"], runs["after"]

    print(f"before  {args.before.name}")
    print(f"after   {args.after.name}\n")

    print(f"{'':34}{'before':>9}{'after':>9}{'Δ':>7}")
    for key, label in (
        ("entities", "实体总数"),
        ("phrase_nodes", "其中像句子的节点"),
        ("linked_to_another_entity", "与另一实体相连的实体"),
        ("linked_pct", "连通比例 %"),
    ):
        before_v, after_v = b["conn"][key], a["conn"][key]
        print(f"  {label:32}{before_v:>9}{after_v:>9}{_delta(before_v, after_v):>7}")
    edges_b, edges_a = len(b["edges"]), len(a["edges"])
    print(f"  {'陈述总数':32}{edges_b:>9}{edges_a:>9}{_delta(edges_b, edges_a):>7}")

    print("\n同一个人被拆成多个 id：")
    for label, run in (("before", b), ("after", a)):
        rows = run["roles"]
        print(f"  {label:8}{('无' if not rows else '')}")
        for role, ids in sorted(rows.items()):
            print(f"      {role}: {ids}")

    print("\n被翻译成英文的节点（中文语料里的纯 ASCII 名，排除 schema 角色）：")
    for label, run in (("before", b), ("after", a)):
        names = run["langs"]
        print(f"  {label:8}{len(names):>4}  {names[:6]}")

    print(f"\n{'类目':22}{'n':>4}{'kg 前':>7}{'kg 后':>7}{'Δ':>6}{'vec 前':>8}{'vec 后':>8}")
    for cat in sorted(set(b["cats"]) | set(a["cats"])):
        bc = b["cats"].get(cat, {"n": 0, "kg": 0, "vec": 0})
        ac = a["cats"].get(cat, {"n": 0, "kg": 0, "vec": 0})
        print(
            f"  {cat:20}{ac['n'] or bc['n']:>4}{bc['kg']:>7}{ac['kg']:>7}"
            f"{_delta(bc['kg'], ac['kg']):>6}{bc['vec']:>8}{ac['vec']:>8}"
        )

    print(
        "\n读法：连通比例是多跳的先决条件，句子节点占比是抽取纪律的直接指标。"
        "\nkg 命中的变化才说明分裂是否真的收敛——总分不适合用来判断抽取改动。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
