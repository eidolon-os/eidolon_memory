# Eidolon Memory 性能报告

- 时间: 2026-05-18T16:24:58.125029+00:00
- Git: ceda3df
- 宫殿: /Users/manson/ai/eidolon/eidolon_memory/reports/bench_palace (drawers≈100)

## read

| 场景 | N | P50(ms) | P95(ms) | P99(ms) | max | 备注 | SLA |
|------|---|---------|---------|---------|-----|------|-----|
| R-01 | 90 | 306.63 | 363.68 | 407.44 | 440.11 | degraded=0.0% | FAIL |
| R-04 | 30 | 46.0 | 60.98 | 64.2 | 88.17 | wing=Wing_Profile | INFO |

## write

| 场景 | N | P50(ms) | P95(ms) | P99(ms) | max | 备注 | SLA |
|------|---|---------|---------|---------|-----|------|-----|
| W-direct | 25 | 26.2 | 30.08 | 30.63 | 31.31 | Worker path (Chroma upsert, no NATS/steward) | PASS |
| W-publish | 25 | 0.17 | 0.51 | - | 1.05 | JetStream publish only (worker ACK not measured) | PASS |

## mixed

| 场景 | N | P50(ms) | P95(ms) | P99(ms) | max | 备注 | SLA |
|------|---|---------|---------|---------|-----|------|-----|
| M-01 |  | - | - | - | - |  | PASS |
