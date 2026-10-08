"""Audit this isolated run through existing storage APIs after its Agent exits."""

import asyncio
import gzip
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from eidolon_memory_contracts import build_memory_actor_context  # noqa: E402

from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter  # noqa: E402
from eidolon.memory.application.public_recall import recall_record_visible_for_context  # noqa: E402
from eidolon.memory.config.memory_settings import MemorySettings  # noqa: E402

OUT = Path(__file__).resolve().parent
SOURCE = ROOT / "reports/memory_quality_20261008_025120"
REALM = "quality-claims3-20261008"
os.environ["MEMPALACE_HOME"] = "/private/tmp/eidolon-quality-20261008-claims3/home"
os.environ["EIDOLON_MEMORY_RUN_DIR"] = "/private/tmp/eidolon-quality-20261008-claims3/run"


async def main():
    import yaml

    settings = MemorySettings.model_validate(
        yaml.safe_load((SOURCE / "spawn_settings.yaml").read_text())
    )
    context = build_memory_actor_context(
        memory_realm_id=REALM,
        owner_id="quality_bench",
        companion_id="quality_bench",
        device_id="quality_bench",
        session_id="quality_bench",
    )
    router = LocalPalaceRouter(settings, allowed_spaces=[REALM])
    try:
        runtime = await router.resolve(REALM)
        records = await runtime.backend.get_all(REALM)
        visible = [r for r in records if recall_record_visible_for_context(r, context)]
        names = await runtime.kg.list_entity_names()
        triples = await runtime.kg.query_subjects(
            names,
            audiences=("owner", "companion:quality_bench"),
            include_sensitive=False,
            limit_per_subject=1000,
        )
        snapshot = {
            "records": [r.model_dump(mode="json") for r in visible],
            "kg_triples": [r.model_dump(mode="json") for r in triples],
        }
        ledger_paths = list((SOURCE / "palaces").glob("*/extraction_decisions.sqlite3"))
        assert len(ledger_paths) == 1
        with sqlite3.connect(f"file:{ledger_paths[0]}?mode=ro", uri=True) as conn:
            decisions = [
                {"turn_id": tid, "decision": json.loads(blob)}
                for tid, blob in conn.execute(
                    "select source_turn_id, decision_json from extraction_decisions "
                    "order by source_turn_id"
                )
            ]
            tombstones = [
                r[0]
                for r in conn.execute("select source_turn_id from extraction_privacy_tombstones")
            ]
    finally:
        await router.aclose()
    raw = json.loads((SOURCE / "raw_results.json").read_text())
    queries = [
        json.loads(s)
        for s in (ROOT / "tests/memory/e2e/fixtures/quality_queries.jsonl").read_text().splitlines()
    ]
    corpus = [
        json.loads(s)
        for s in (ROOT / "tests/memory/e2e/fixtures/companion_corpus.jsonl")
        .read_text()
        .splitlines()
    ]
    by_id = {r["id"]: r for r in raw["per_query"]}
    misses = []
    for q in queries:
        result = by_id[q["id"]]
        if q.get("expect_abstention"):
            misses.append(
                {
                    "id": q["id"],
                    "category": q["category"],
                    "group": "abstention",
                    "returned": result["returned_evidence_count"],
                    "forbidden_violation": result["negative_violation"],
                }
            )
            continue
        for group, field, hit in [
            ("vector", "expected_vector_contains", "vector_hit"),
            ("kg", "expected_entities", "kg_hit"),
        ]:
            if not q.get(field) or result[hit]:
                continue
            terms = q[field]
            if group == "vector":
                matches = [
                    r for r in snapshot["records"] if any(t in str(r["value"]) for t in terms)
                ]
            else:
                matches = [
                    r
                    for r in snapshot["kg_triples"]
                    if any(
                        t.lower() in (r["subject"] + " " + str(r["object"])).lower() for t in terms
                    )
                ]
            misses.append(
                {
                    "id": q["id"],
                    "category": q["category"],
                    "group": group,
                    "expected_any": terms,
                    "present_in_visible_projection": bool(matches),
                    "projection_matches": matches,
                }
            )
    account = {r["turn_id"] for r in decisions} | set(tombstones)
    assert account == {r["turn_id"] for r in corpus}
    assert raw["aggregate"]["overall"]["n"] == len(queries) == 48
    assert raw["aggregate"]["overall"]["valid"]
    report = {
        "coverage": {
            "published": len(corpus),
            "retained_decisions": len(decisions),
            "privacy_tombstones": tombstones,
            "all_turns_accounted": True,
        },
        "visible_records": len(visible),
        "active_nonsensitive_triples": len(triples),
        "missing_groups": misses,
    }
    for name, value in [
        ("projection.json.gz", snapshot),
        ("decisions.json.gz", decisions),
        ("failure-analysis.json", report),
    ]:
        data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        (OUT / name).write_bytes(gzip.compress(data, mtime=0) if name.endswith(".gz") else data)
    print(report["coverage"])
    print(
        "missing positive groups",
        sum(m["group"] != "abstention" for m in misses),
        "already in visible projection",
        sum(m.get("present_in_visible_projection", False) for m in misses),
    )


if __name__ == "__main__":
    asyncio.run(main())
