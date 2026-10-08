"""Summarize the raw MCP scale runs without changing samples or percentile rules."""

import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.benchmark.report import percentiles  # noqa: E402

OUT = Path(__file__).resolve().parent
summary = []
violations = []
for path in sorted([*OUT.glob("[0-9]*-b[12].json"), *OUT.glob("[0-9]*-b[12].json.gz")]):
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    data = json.loads(raw)
    samples = data["per_call"]
    measured = [s for s in samples if not s["warmup"]]
    for sample in samples:
        for row in sample["records"]:
            if row["memory_space_id"] != "context-close-scale":
                violations.append([path.name, "realm", row["key"]])
            match = re.fullmatch(r"fact-(\d+)", row["key"])
            if match and any(int(match[1]) % divisor == 0 for divisor in (37, 41, 43)):
                violations.append([path.name, "restricted-record", row["key"]])
    stages = sorted({k for s in measured for k in s["trace"]})
    summary.append(
        {
            "run": path.name.removesuffix(".gz").removesuffix(".json"),
            "sla": data["sla"],
            "budget_ms": data["sla_p95_ms"],
            "summary": data["summary"],
            "warmup": data["warmup"],
            "session_error": data["startup_error"],
            "stages_ms": {
                k: percentiles([s["trace"][k] for s in measured if k in s["trace"]]) for k in stages
            },
            "client_minus_service_ms": percentiles(
                [
                    s["elapsed_ms"] - s["trace"]["service_total_ms"]
                    for s in measured
                    if "service_total_ms" in s["trace"]
                ]
            ),
        }
    )
result = {"runs": summary, "restricted_vector_records_returned": violations}
(OUT / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
assert len(summary) == 6
assert not violations
assert all(
    r["summary"]["errors"] == r["summary"]["degraded"] == r["summary"]["empty"] == 0
    and r["warmup"]["errors"] == r["warmup"]["degraded"] == 0
    and r["session_error"] is None
    for r in summary
)
for size in (5000,):
    for mode in ("chat", "chat-graph", "voice"):
        runs = [
            r
            for r in summary
            if r["run"].startswith(f"{size}-{mode}-")
            and ("-graph-" in r["run"]) == (mode == "chat-graph")
        ]
        values = [r["summary"]["latency_ms"]["p95"] for r in runs]
        print(
            size,
            mode,
            f"p95 {min(values):.1f}–{max(values):.1f} ms",
            f"PASS {sum(r['sla'] == 'PASS' for r in runs)}/{len(runs)}",
        )
