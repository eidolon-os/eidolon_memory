"""Inspect score overlap in frozen top-k; not an end-to-end threshold simulation."""

import gzip
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
raw = json.loads(gzip.decompress((OUT / "raw_results.json.gz").read_bytes()))
labels = {
    q["id"]: q
    for q in map(
        json.loads,
        (ROOT / "tests/memory/e2e/fixtures/quality_queries.jsonl").read_text().splitlines(),
    )
}
negative, positive = [], []
for row in raw["raw_responses"]:
    q = labels[row["id"]]
    records = row["response"].get("records", [])
    scored = [(r, r.get("metadata", {}).get("similarity")) for r in records]
    scored = [(r, float(s)) for r, s in scored if isinstance(s, (int, float))]
    if q.get("expect_abstention"):
        negative.append(
            {
                "id": row["id"],
                "max_similarity": max((s for _, s in scored), default=None),
                "kg_count": len(row["response"].get("kg_triples", [])),
            }
        )
    elif q.get("expected_vector_contains"):
        matches = [
            (r, s)
            for r, s in scored
            if any(term in r["value"] for term in q["expected_vector_contains"])
        ]
        positive.append(
            {
                "id": row["id"],
                "best_label_match_similarity": max((s for _, s in matches), default=None),
            }
        )
negative_ceiling = max(r["max_similarity"] for r in negative if r["max_similarity"] is not None)
positive_floor = min(
    r["best_label_match_similarity"]
    for r in positive
    if r["best_label_match_similarity"] is not None
)
report = {
    "negative": negative,
    "positive_vector_labels": positive,
    "negative_max": negative_ceiling,
    "positive_min_of_best_matches": positive_floor,
    "one_threshold_separates_these_returned_records": negative_ceiling < positive_floor,
    "limitations": (
        "Frozen top-k, substring labels, no new candidates, no KG filtering; "
        "not calibrated held-out evaluation."
    ),
}
(OUT / "relevance-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(negative_ceiling, positive_floor)
