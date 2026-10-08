"""Frozen vectors + copied real KG: change only the graph expansion strategy."""

import ast
import asyncio
import gzip
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from eidolon.memory.adapters import kg_sqlite  # noqa: E402
from eidolon.memory.application.kg_recall import (  # noqa: E402
    expand_from_recalled,
    query_kg_for_recall,
)
from eidolon.memory.application.public_recall import _merge_triples  # noqa: E402
from eidolon.memory.domain.space_lock import SpaceLock  # noqa: E402
from scripts.benchmark.bench_memory_retrieve_quality import _aggregate, _score_query  # noqa: E402

OUT = Path(__file__).resolve().parent
SOURCE = ROOT / "benchmarks/results/unified-claims-20261008/raw_results.json.gz"
LEDGER = next(
    (ROOT / "reports/memory_quality_20261008_025120/palaces").glob("*/knowledge_graph.sqlite3")
)
BASE = "36cc2f5"
# Execute only the two recorded old methods, against the copied experimental graph.
source = subprocess.check_output(
    ["git", "show", f"{BASE}:eidolon/memory/adapters/kg_sqlite.py"], cwd=ROOT
).decode()
cls = next(
    n
    for n in ast.parse(source).body
    if isinstance(n, ast.ClassDef) and n.name == "SqliteKnowledgeGraph"
)
methods = [
    n
    for n in cls.body
    if getattr(n, "name", "") in {"entities_for_source_turns", "_entities_for_turns_sync"}
]
namespace = dict(vars(kg_sqlite))
exec(
    compile(ast.Module(body=methods, type_ignores=[]), "<recorded-baseline-methods>", "exec"),
    namespace,
)


class LegacyGraph(kg_sqlite.SqliteKnowledgeGraph):
    _entities_for_turns_sync = namespace["_entities_for_turns_sync"]

    async def entities_for_source_turns(self, turn_ids, *, cap, **kwargs):
        return await namespace["entities_for_source_turns"](self, turn_ids, cap=cap)


async def main():
    raw = json.loads(gzip.decompress(SOURCE.read_bytes()))
    labels = OUT / "labels.jsonl"
    queries = [json.loads(s) for s in labels.read_text().splitlines()]
    responses = {r["id"]: r["response"] for r in raw["raw_responses"]}
    audiences = ("owner", "companion:quality_bench")
    moment = "2026-10-08T02:56:49Z"
    scores, details = {}, {}
    with tempfile.TemporaryDirectory(prefix="eidolon-kg-ablation-") as directory:
        copied = Path(directory) / "kg.sqlite3"
        with (
            sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True) as origin,
            sqlite3.connect(copied) as target,
        ):
            origin.backup(target)
        for variant, kind in [
            ("baseline", LegacyGraph),
            ("scoped", kg_sqlite.SqliteKnowledgeGraph),
            ("no_expansion", kg_sqlite.SqliteKnowledgeGraph),
        ]:
            graph = kind(copied, space_id="quality-claims3-20261008", lock=SpaceLock())
            scores[variant], details[variant] = [], []
            try:
                for q in queries:
                    frozen = responses[q["id"]]
                    turn_ids = list(
                        dict.fromkeys(r["metadata"]["source_turn_id"] for r in frozen["records"])
                    )
                    entities = await graph.match_entities_for_query(
                        q["query"], audiences=audiences, cap=3
                    )
                    primary = await query_kg_for_recall(
                        graph,
                        audiences=audiences,
                        entity_names=entities,
                        now_iso=moment,
                        max_triples_per_entity=8,
                    )
                    expanded = (
                        []
                        if variant == "no_expansion"
                        else await expand_from_recalled(
                            graph,
                            audiences=audiences,
                            source_turn_ids=turn_ids,
                            now_iso=moment,
                            max_entities=3,
                            max_triples_per_entity=8,
                        )
                    )
                    merged = _merge_triples(
                        primary, expanded, already_shown=set(turn_ids), limit=24
                    )
                    if variant == "baseline":
                        assert [t.id for t in merged] == [t["id"] for t in frozen["kg_triples"]], q[
                            "id"
                        ]
                    response = {**frozen, "kg_triples": [t.model_dump(mode="json") for t in merged]}
                    # These labels do not grade a dated supersession rendering.
                    # Recompute graph/vector evidence metrics, not model answers or latency.
                    scores[variant].append(_score_query(q, response, 0))
                    details[variant].append(
                        {
                            "id": q["id"],
                            "phrase_entities": entities,
                            "primary_ids": [t.id for t in primary],
                            "expanded_ids": [t.id for t in expanded],
                            "merged_ids": [t.id for t in merged],
                            "seeds": []
                            if variant == "no_expansion"
                            else await graph.entities_for_source_turns(
                                turn_ids,
                                cap=3,
                                audiences=audiences,
                                include_sensitive=False,
                            ),
                        }
                    )
            finally:
                graph.close()
    aggregate = {k: _aggregate(v) for k, v in scores.items()}
    for value in aggregate.values():
        for row in [value["overall"], *value["per_category"]]:
            for key in list(row):
                if key.endswith("_ms"):
                    del row[key]
    report = {
        "base_commit": BASE,
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "labels_sha256": hashlib.sha256(labels.read_bytes()).hexdigest(),
        "as_of": moment,
        "baseline_reproduced": 48,
        "aggregate": aggregate,
        "details": details,
        "correctness_changes": {
            k: [
                {"id": a.id, "before": a.correct, "after": b.correct}
                for a, b in zip(scores["baseline"], v, strict=True)
                if a.correct != b.correct
            ]
            for k, v in scores.items()
        },
        "kg_row_counts": {k: sum(len(r["merged_ids"]) for r in v) for k, v in details.items()},
    }
    (OUT / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v["overall"] for k, v in aggregate.items()}, ensure_ascii=False, indent=2))
    print(report["kg_row_counts"], report["correctness_changes"])


if __name__ == "__main__":
    asyncio.run(main())
