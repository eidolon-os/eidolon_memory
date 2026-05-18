"""Performance report helpers for memory benchmarks."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


def percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "min": 0.0}
    ordered = sorted(samples)
    n = len(ordered)

    def pct(p: float) -> float:
        if n == 1:
            return ordered[0]
        idx = min(n - 1, max(0, int(p * n) - 1))
        return ordered[idx]

    return {
        "count": n,
        "min": ordered[0],
        "p50": pct(0.50),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": ordered[-1],
        "mean": statistics.mean(ordered),
    }


def sla_pass(p50: float, p95: float, *, p50_max: float, p95_max: float) -> bool:
    return p50 <= p50_max and p95 <= p95_max


def write_report(
    out_dir: Path,
    meta: dict[str, Any],
    sections: dict[str, dict[str, Any]],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = {"meta": meta, **sections}
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    lines = [
        "# Eidolon Memory 性能报告",
        "",
        f"- 时间: {meta.get('timestamp', '')}",
        f"- Git: {meta.get('git', 'n/a')}",
        f"- 宫殿: {meta.get('palace', '')} (drawers≈{meta.get('drawer_hint', 'n/a')})",
        "",
    ]
    for title, data in sections.items():
        lines.append(f"## {title}")
        lines.append("")
        if "table" in data:
            lines.append("| 场景 | N | P50(ms) | P95(ms) | P99(ms) | max | 备注 | SLA |")
            lines.append("|------|---|---------|---------|---------|-----|------|-----|")
            for row in data["table"]:
                note = row.get("note", "")
                if row.get("degraded_pct") is not None:
                    note = f"degraded={row.get('degraded_pct')}% {note}".strip()
                if row.get("wing"):
                    note = f"wing={row['wing']} {note}".strip()
                lines.append(
                    f"| {row['id']} | {row.get('n', '')} | {row.get('p50', '-')} | "
                    f"{row.get('p95', '-')} | {row.get('p99', '-')} | {row.get('max', '-')} | "
                    f"{note} | {row.get('sla', '')} |"
                )
        lines.append("")

    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
