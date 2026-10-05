"""Rescore frozen MCP responses; no new retrieval or model calls."""

import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.benchmark.bench_memory_retrieve_quality import _aggregate, _score_query  # noqa: E402

OUT = Path(__file__).resolve().parent
QUERY_PATH = "tests/memory/e2e/fixtures/quality_queries.jsonl"
SOURCE = OUT.parent / "quality-current-20261005/raw_results.json.gz"
raw = json.loads(gzip.decompress(SOURCE.read_bytes()))
responses = {r["id"]: r["response"] for r in raw["raw_responses"]}
elapsed = {r["id"]: r["elapsed_ms"] for r in raw["per_query"]}
labels = {
    "before": subprocess.check_output(["git", "show", f"4384438:{QUERY_PATH}"], cwd=ROOT),
    "after": (ROOT / QUERY_PATH).read_bytes(),
}
scored = {}
for variant, content in labels.items():
    queries = [json.loads(line) for line in content.splitlines()]
    scored[variant] = [_score_query(q, responses[q["id"]], elapsed[q["id"]]) for q in queries]
report = {
    "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
    "labels_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in labels.items()},
    "aggregate": {k: _aggregate(v) for k, v in scored.items()},
    "changed_labels": [],
}
old = {q["id"]: q for q in map(json.loads, labels["before"].splitlines())}
for q, before, after in zip(
    map(json.loads, labels["after"].splitlines()), scored["before"], scored["after"], strict=True
):
    if q != old[q["id"]]:
        report["changed_labels"].append(
            {
                "id": q["id"],
                "before": old[q["id"]],
                "after": q,
                "correct_before": before.correct,
                "correct_after": after.correct,
            }
        )
(OUT / "rescore-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
for k, v in report["aggregate"].items():
    print(k, v["overall"])
