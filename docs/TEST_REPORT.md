# Test report

Measured on 2026-08-01, branch `refactor/memory-contracts-v2`, commit `9f0d762`.
macOS 15.5 / Apple Silicon, Python 3.12, MemPalace 3.6.0.

Every number here comes from a run on this machine. Where a claim is not tested,
it says so rather than being left out — an untested claim that looks tested is
the failure this document exists to prevent.

Reproduce with:

```bash
uv sync --all-extras
```

---

## Summary

| Category | Tests | Result | Notes |
|---|---|---|---|
| Unit | 792 | **792 passed, 6 skipped** | 79% line coverage |
| Functional (e2e) | 35 | **25 passed, 2 failed, 8 skipped** | 2 pre-existing LLM extraction failures |
| Local↔cloud switch | 113 | **113 passed** | Same tests, both storages |
| Contract | 128 | **128 passed** | 46 standalone + 82 in-repo |

The two e2e failures are a steward extraction shortfall that predates this work
and is unrelated to it — see [Functional](#functional-tests-e2e).

---

## Unit tests

```bash
uv run pytest tests/memory --ignore=tests/memory/e2e -q --cov=eidolon.memory
```

**792 passed, 6 skipped, 92s. 8682 statements, 79% covered.**

The 6 skips are MemPalace-marked tests needing a real palace on disk.

### Coverage by layer

| Layer | Coverage | Reading |
|---|---|---|
| `domain/` | 96% | Protocols and models — near-total, as it should be |
| `application/` | 83% | Recall and turn logic |
| `adapters/` | 74% | Held down by the MemPalace adapter's error paths |
| `infrastructure/` | 83% | |
| `config/` | 90% | |
| `support/` | 98% | |
| `entrypoints/` | 53% | Process wiring, covered by e2e instead |

### Where coverage is low, and whether that is acceptable

| Module | Cov | Judgement |
|---|---|---|
| `entrypoints/agent_runner.py` | 0% | **Acceptable** — it is a process entrypoint; e2e runs it as a subprocess, which coverage cannot see |
| `application/mempalace_hierarchy.py` | 20% | **Not acceptable** — this is library code, not wiring. Untested |
| `infrastructure/nats/commands.py` | 33% | **Not acceptable** — command dispatch is exercised only through e2e |
| `adapters/mempalace_fast_search.py` | 44% | **Partly** — the uncovered half is the hot-path bypass, covered by benchmarks rather than tests |
| `adapters/kg_postgres.py` | 66% | **Acceptable now** — the uncovered part is the connect/pool path; every query is exercised by the live suite |

---

## Functional tests (e2e)

```bash
uv run pytest tests/memory/e2e -q
```

**25 passed, 2 failed, 8 skipped, ~15 min.** Each test starts a real
`nats-server` and a real `eidolon-memory-agent` subprocess and drives the whole
path: publish a turn → steward → projection → recall → forget → confirm.

The 8 skips need an LLM endpoint (`EIDOLON_MEMORY_LLM_API_KEY`).

### The two failures

```
test_consolidator_lifecycle.py::test_consolidator_subprocess_produces_wing_theme_drawers
test_entity_mention_resolution.py::test_llm_extracts_mentions_and_alias_query_routes_via_kg
  kg_stats={'entities': 2, 'triples_total': 1, 'triples_active': 1, 'mentions': 1}
```

Both are the same defect: the LLM steward extracts 1–3 triples from a 12-turn
conversation where the test expects ≥8. It predates this refactor, is present on
the base commit, and the count varies run to run (5/3/2 one run, 2/1/1 the next),
which is what identifies it as model instability rather than a code regression.

It is listed as an open defect rather than quarantined, because it blocks any
graph-dependent quality benchmark from meaning anything.

---

## Local↔cloud switch tests

```bash
uv run pytest tests/memory/test_deployment_profiles.py \
  tests/memory/test_router_contract.py tests/memory/test_ledger_contract.py \
  tests/memory/test_kg_dialects.py tests/memory/test_live_postgres_kg.py -q
```

**113 passed, 59s.**

This is the category where a passing test is easiest to fake, so what each suite
actually proves is spelled out.

| Suite | Tests | What it proves |
|---|---|---|
| `test_router_contract` | 29 | Both routers satisfy one interface, and the **one asymmetry**: embedded storage refuses a second holder, shared storage serves the same space from two replicas concurrently |
| `test_ledger_contract` | 47 | Every ledger behaviour asserted against **both** SQLite and PostgreSQL |
| `test_live_postgres_kg` | 11 | The graph against a **real server**, not a mock |
| `test_kg_dialects` | 14 | The two dialects build structurally identical statements |
| `test_deployment_profiles` | 12 | Local and cloud config files carry the same field set |

### What "seamless" is verified to mean

Changing storage is a configuration edit, with **no code change**, for:

| Axis | Switch | Verified by |
|---|---|---|
| Vector: chroma ↔ milvus | `mempalace.backend` | Live milvus (8.140.214.42, `eidolon` db) |
| Graph: none ↔ sqlite ↔ postgres | `kg.backend` | 11 live PG tests + round-trip e2e |
| Ledgers: palace ↔ postgres | `ledgers.backend` | 47 contract tests, both storages |
| Deployment shape | *derived from storage config* | `test_router_contract` |

Deployment shape has no flag of its own: `build_space_router` derives it from
where storage lives, so a config cannot say "cloud" while pointing at a local
directory.

### What is not switchable yet

**Three of six ledgers have no PostgreSQL implementation**: `commitments`,
`canonical_facts`, `command_status`. On shared storage the router hands back
`None` for them and each consumer's existing `None` handling keeps the service
running — so it starts and serves recall, but:

- a corrected fact is not invalidated (`canonical_facts` holds that chain);
- commitment queries return empty;
- `command_status` cannot report on an async write.

The first two are product behaviour, not bookkeeping. This is stated in the
router's log line at startup and here, and it is the largest remaining gap.

### PostgreSQL testing

`pgserver` ships PostgreSQL binaries as a wheel, so the shared-storage suites run
a real server with no container and no system package. Before this, the PG paths
were verified only structurally and the 11 tests that would have proved otherwise
were gated behind an environment variable nobody set.

Point `EIDOLON_MEMORY_PG_TEST_DSN` at another server to check a specific version.
Each test gets its own schema, dropped afterwards.

---

## Contract tests

**128 passed.** Two groups.

### The distributable contracts package (46)

```bash
cd contracts && uv run --isolated --with pydantic --with pytest --with pytest-asyncio pytest tests -q
```

Run with **only pydantic installed** — no MemPalace, no chromadb, no onnxruntime.
That is the point: a client speaking the protocol must not need the service's
storage stack. The isolated run is the proof.

### In-repo contract and boundary tests (82)

| Suite | Tests | Enforces |
|---|---|---|
| `test_kg_sqlite` | 41 | The graph port's behaviour |
| `test_backend_contract` | 14 | Vector port, and the 5-field hot-path minimum |
| `test_layering` | 10 | Every ledger satisfies its port; the logic layer imports no storage library and compares no backend name |
| `test_backend_capabilities` | 11 | Warming and room enumeration are optional and correct when absent |
| `test_os_import_boundary` | 4 | Core imports no `eidolon_*` package — static scan plus a subprocess load with OS packages blocked |
| `test_lazy_import_guard` | 2 | No unjustified deferred imports |

The two boundary suites were sabotage-verified: a violation was injected
temporarily to confirm each actually fails.

### The gap in this category

`MemoryReadContract` and `MemoryWriteContract` are defined and tested, but
**nothing implements them**. A grep across memory, agent, and admin finds them
only in the contracts package's own tests. The read path has no typed contract in
practice: the agent defines its own `MemoryRecallResult` and consumes MCP tool
JSON.

What is actually load-bearing today is the wire layer — subjects, envelope,
payload, and `MemoryActorContext` — of which the agent uses five symbols, all on
the write path.

So this suite proves the contracts are *self-consistent*, not that they are *in
use*. Closing that is the next piece of work.

---

## Standing test properties

- **No test is skipped for a missing dependency it could provide itself.** psycopg
  and pgserver are dev dependencies, not just extras: optional to run the service,
  mandatory to test it. A skipped suite reads as a passing one.
- **Differences between local and cloud are asserted, not avoided.** A test that
  passed against both by steering around what separates them would be the most
  misleading kind of green. Both known asymmetries — the second-holder rule and
  DLQ claim recovery — have tests that pin them in each direction.
- **Guard tests are sabotage-verified.** A guard that cannot fail is decoration.
