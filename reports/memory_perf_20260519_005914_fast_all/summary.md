# Eidolon Memory 性能报告

- 时间: 2026-05-18T16:59:14.635485+00:00
- Git: ceda3df
- 宫殿: reports/bench_palace (drawers≈100)

## read

| 场景 | N | P50(ms) | P95(ms) | P99(ms) | max | 备注 | SLA |
|------|---|---------|---------|---------|-----|------|-----|
| R-01 | 90 | 16.08 | 22.26 | 49.02 | 63.85 | degraded=0.0% | PASS |
| R-04 | 50 | 1.92 | 2.48 | 3.66 | 26.33 | wing=Wing_Profile | INFO |

## write

| 场景 | N | P50(ms) | P95(ms) | P99(ms) | max | 备注 | SLA |
|------|---|---------|---------|---------|-----|------|-----|
| W-direct |  | - | - | - | - | read-only run | SKIP |
| W-publish |  | - | - | - | - | read-only run | SKIP |

## mixed

| 场景 | N | P50(ms) | P95(ms) | P99(ms) | max | 备注 | SLA |
|------|---|---------|---------|---------|-----|------|-----|
| M-01 |  | - | - | - | - | read-only run | SKIP |
