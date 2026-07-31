# Test suite baseline — before the contracts/storage refactor

Recorded on branch `refactor/memory-contracts-v2` at commit 0585efa (pre-refactor tree).

```
uv run pytest tests/ -q
559 passed, 2 failed, 10 skipped in 972.14s (16:12)
```

## Pre-existing failures (not introduced by the refactor)

Both are LLM-backed e2e tests, and both point at the same defect: the LLM
steward extracts far fewer KG triples than the pipeline expects.

| Test | Symptom |
|---|---|
| `tests/memory/e2e/test_entity_mention_resolution.py::test_llm_extracts_mentions_and_alias_query_routes_via_kg` | steward produced 1 triple / 2 entities in 180s; test requires ≥8 |
| `tests/memory/e2e/test_consolidator_lifecycle.py::test_consolidator_subprocess_produces_wing_theme_drawers` | no Wing_Theme drawers produced |

The LLM is reachable (it did produce a triple and two entities), so this is an
extraction-quality defect rather than a broken endpoint. It matches what the
most recent quality benchmark showed: `triples_total=0`, with the
`canonical_entity` and `kinship_alias` question categories scoring 0%.

Tracked as its own workstream — steward prompt and model capability
(`openai/deepseek-v4-flash`) both need evaluating. It does not block the
structural refactor, which changes no steward behaviour.

## What this baseline is for

The 559 passing tests are the regression net for the structural work (contract
package extraction, mempalace containment, port abstraction). Any drop in that
count is a regression introduced by the refactor.

Latency and quality baselines are separate and must be rebuilt before the
recall-performance work — see the plan's S0.
