"""Compare frozen responses under identical labels; no model or service calls."""

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.benchmark.bench_memory_retrieve_quality import _aggregate, _score_query  # noqa: E402

OUT = Path(__file__).resolve().parent
labels = ROOT / "tests/memory/e2e/fixtures/quality_queries.jsonl"
queries = [json.loads(s) for s in labels.read_text().splitlines()]
paths = {
    "before": OUT.parent / "quality-current-20261005/raw_results.json.gz",
    "after": OUT / "raw_results.json.gz",
}
scores = {}
for variant, path in paths.items():
    raw = json.loads(gzip.decompress(path.read_bytes()))
    responses = {r["id"]: r["response"] for r in raw["raw_responses"]}
    times = {r["id"]: r["elapsed_ms"] for r in raw["per_query"]}
    scores[variant] = [_score_query(q, responses[q["id"]], times[q["id"]]) for q in queries]
report = {
    "label_sha256": hashlib.sha256(labels.read_bytes()).hexdigest(),
    "sources_sha256": {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in paths.items()},
    "aggregate": {k: _aggregate(s) for k, s in scores.items()},
    "changed": [
        {"id": a.id, "before": a.correct, "after": b.correct}
        for a, b in zip(scores["before"], scores["after"], strict=True)
        if a.correct != b.correct
    ],
    "negative_violations": {
        k: [r.id for r in s if r.negative_violation] for k, s in scores.items()
    },
}
(OUT / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
