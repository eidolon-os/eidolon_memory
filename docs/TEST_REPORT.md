# Test report

## MemPalace 3.8 release gate — 2026-09-01

Measured on macOS / Apple Silicon with Python 3.13 and `mempalace==3.8.0`.
The final commit and Pi release identity are recorded after integration; no Pi
result is claimed by this local gate.

| Category | Result | Notes |
|---|---|---|
| Memory non-E2E | **1152 passed, 5 skipped** | Includes public-provider hardening and exact source-event cleanup for partial materialisation; 3 local-port tests were rerun outside the restricted sandbox |
| Memory real-process E2E | **21 passed, 8 skipped, 3 deselected** | Real NATS, Memory subprocesses, Chroma, restart, redelivery, privacy, snapshot/restore and shared/per-wing read-path parity; skipped cases require a live Pi Realm or external LLM |
| Configuration / embedder contract | **141 passed** | Includes public-only MemPalace provider configuration and the BGE provider contract |
| Standalone wire contracts | **61 passed** | Isolated environment with no Memory storage stack installed |
| Changed-file Ruff / compile | **passed** | No private compatibility module or process-wide model-download patch remains |

After removing both the explicit-claim language router and Eidolon's CJK
full-drawer fallback, the complete suite was run again outside the restricted
sandbox (the real-process cases bind loopback ports): **1159 passed, 13 skipped,
3 deselected in 14m27s**.  Structured triples obtain intent type, wing and memory
type from the single predicate registry; a verbatim administrative intent
without an explicit destination fails closed.  A syntax-tree regression test
prevents that projection path from reading `raw_claim` in order to infer a
destination.

### Recall evidence and open integration gates

MemPalace 3.8 Chroma lexical search does not segment ordinary short CJK queries:
a fresh Palace returned no lexical hit for `曼森`, `名字`, `我叫什么名字`, `芒果`,
`水果`, or `最喜欢的水果是什么`, including with a valid audience filter.  A
1003-drawer BGE-small-zh-v1.5 benchmark also showed that vector top-five alone is
not a completeness guarantee for those prompts.

That limitation is **not an absolute blocker for canonical long-term facts**.
Eidolon facts are sourced from the assertion/evidence ledger and projected into
the semantic KG.  A real-process Agent E2E now asks the ordinary question
`我最喜欢的水果是什么？` without putting the answer marker in the query.  After
deleting Eidolon's CJK n-gram/full-drawer scan, all 60 recalls resolved the
authenticated `self` fact through the product query plan.  Write visibility was
37.135 ms; Agent recall p50/p95/p99/max was 10/16/34/41 ms, and compiler total
was 11.056/16.701/34.475/41.278 ms against a 500 ms budget.

The remaining CJK limitation is explicitly scoped to **unstructured narrative
recall quality**.  It stays in the held-out MemPalace/vector benchmark and must
not be hidden by an Eidolon tokenizer, private SQLite query, full-drawer scan,
or sidecar index.  A narrative miss degrades that projection; it does not create
a second fact source or justify a duplicate search engine.

The integrated Host gate still waits for the Channel turn-decision authority
work and the commissioning source set to be integrated and released together.
The Pi currently reports every service running with zero restarts, but its
active older release remains degraded at `hub_admits_devices=false`.

### After the release gate — 2026-09-01, local only

Two things the gate could not see were closed, and both were verified locally
only.  No Pi run is claimed here: the Host stayed on
`20260901-memory380-e2e-fe60160-40b3550-v4` throughout, 18/18 services
active/running with zero restarts, and nothing was deployed.

Memory **1182 passed, 13 skipped, 3 deselected in 15m11s**; Agent **602 passed,
1 skipped**.  Changed-file Ruff clean in both.

1. **Turn latency is attributable.**  Only the steward stage was measured, so
   the gate run's 59.25s materialisation — against 18.1s / 27.7s / 27.6s before
   it — could be observed and not explained.  Four stages are now recorded
   (`steward`, `privacy`, `fragments`, `kg`) plus a `total`, and the same
   numbers go onto the `turn_processed` log line, because a percentile cannot
   name the one turn an outlier is about.  **The 59s itself is still
   unattributed**: attributing it needs a live run, which this pass did not
   make.
2. **The materialisation budget is a gate.**  The live contract reported every
   readback under `memory_readback_timeout_s` as the same `passed`, so an 18s
   write and a 59s write were indistinguishable in the report.
   `--memory-materialization-budget-s` now fails the check while still
   reporting the record that landed.  **Unit-tested, not yet exercised against
   the Pi.**
3. **The MCP tool surface is no longer one space.**  24 of 28 tools closed over
   the process's handles; all 28 now resolve through the router per request,
   with an optional `memory_space_id` that falls back to the server's own.  No
   caller changes.  This removes the blocker `ARCHITECTURE.md` recorded for
   `进程 : space = 1 : N`; what remains there is deployment topology, which is
   deferred, and the wildcard-subject consumer, which changes a published
   runtime topology and so needs a real Pi E2E before it can be claimed.

The CJK release blockers above are unchanged.  Nothing in this pass touches
MemPalace's tokenizer, and none of it makes the integrated Pi release eligible.

Cross-repository gates run against the final merged source set: Agent **605
passed, 1 skipped** and its live contract harness **13/13**; Channel **1637
passed, 7 skipped, 25 deselected**; Mobile **671 passed, 5 skipped**. Mobile had
unrelated device-commissioning work in progress and was tested read-only.

The deterministic real-process recall gate uses exact projected text. Its
offline hash embedder intentionally has no semantic meaning, so paraphrase
quality is measured only by the real-embedder benchmark; the E2E gate proves
transport, scope, Chroma and ranking-path parity without pretending that a hash
vector measures product recall quality.

## Historical 3.6 baseline

Measured on 2026-08-01, branch `refactor/memory-contracts-v2`, commit `9f0d762`.
macOS 15.5 / Apple Silicon, Python 3.12, MemPalace 3.6.0. The sections below are
the retained baseline; where a 3.8 configuration row is updated, it is called
out explicitly rather than presenting the baseline as a new run.

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
| Unit | 820 | **818 passed, 2 skipped** | Was 830/6; the cloud removal took its tests and every skip was a PostgreSQL one |
| Functional (e2e) | 35 | **needs a rerun** | Changing the embedder rebuilds the index. The quality bench *has* been rerun on it: 21/49, p95 30 ms — see below |
| Configuration switch | 97 | **97 passed** | Was 144 across two storages; see below for what the category is now |
| Contract | 133 | **133 passed** | 46 standalone + 87 in-repo |

The two e2e failures are a steward extraction shortfall that predates this work
and is unrelated to it — see [Functional](#functional-tests-e2e).

---

## Unit tests

```bash
uv run pytest tests/memory --ignore=tests/memory/e2e -q --cov=eidolon.memory
```

**830 passed, 6 skipped, 101s. 8682 statements, 79% covered.**

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

---

## Functional tests (e2e)

```bash
uv run pytest tests/memory/e2e -q
```

**25 passed, 2 failed, 8 skipped, 16m 05s.** Each test starts a real
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
conversation where the test expects ≥8. It predates this refactor and is present
on the base commit.

What identifies it as model instability rather than a code regression is that the
count moves between runs of identical code. Two consecutive runs this session
reported `entities=2, triples=1` and then `entities=5, triples=3,
invalidated=1` — the second shows the invalidation chain working, which a code
fault would not do intermittently.

It is listed as an open defect rather than quarantined, because it blocks any
graph-dependent quality benchmark from meaning anything.

### What this run also confirmed

An earlier run of this suite showed **three** failures, and the third was mine:
warmup had stopped running because the capability check could not see through
`LockedBackend`. Nothing in the unit suite noticed — warming is best-effort, so
skipping it raises nothing. It appeared only here, as recall's graph lookup
exceeding its 300ms budget while the embedding model loaded on the first request,
in a test whose every *direct* graph assertion passed.

That is the argument for keeping this suite: it runs the real process with the
real wrappers, and it is the only category that would have caught this.

---

## Configuration switch tests

```bash
uv run pytest tests/memory/test_deployment_profiles.py \
  tests/memory/test_router_contract.py tests/memory/test_ledger_contract.py \
  tests/memory/test_kg_optional.py tests/memory/test_local_embedder.py \
  tests/memory/test_mempalace_backend_config.py -q
```

**141 passed, 11.84s.**

This category was "local↔cloud switch": the same behavioural tests run against
SQLite ledgers and PostgreSQL ones, against a Chroma file and a live Milvus, to
prove that moving between one machine and many was a config change. The cloud
implementations have been removed, so that claim is no longer made and those
tests are gone with the code they covered.

What remains is the switching that a local deployment actually does, and it is
worth its own category for the same reason as before: these are the settings
where a wrong value produces a *working* service that answers differently.

| Suite | Tests | What it proves |
|---|---|---|
| `test_ledger_contract` | 53 | Every ledger behaviour asserted against the port, never against SQLite — including opening a file written before the space column existed |
| `test_local_embedder` | 61 | The embedder seam: three implementations behind one port, the Chroma shape living in one adapter instead of on the encoder, our encoder being what MemPalace resolves (verified through its public function, not assumed from the cache key), pooling and prefixes per family, a hosted endpoint's batching / retry / declared width, the config migration, and that `infrastructure` does not import `adapters` |
| `test_router_contract` | 13 | The router contract, and that a palace refuses a second holder |
| `test_kg_optional` | 6 | `kg.backend: none` serves recall vector-only, offers no graph tools, and deletes nothing |
| `test_mempalace_backend_config` | 12 | Only public MemPalace provider settings are exported; unsupported native-provider `model_dir` fails at configuration time |
| `test_deployment_profiles` | 5 | The shipped template loads, names its embedder, and keeps secrets in variables |

### What is verified to be a config change

| Axis | Switch | Verified by |
|---|---|---|
| **Embedder implementation: in-process ONNX ↔ hosted HTTP endpoint ↔ MemPalace's own** | `embedding.provider` | `test_local_embedder` — same factory call, three classes, and the registration path holding a hosted embedder without knowing it is one. Exercised for real: a palace built end to end against an OpenAI-compatible endpoint, Chroma persisting the declared width, and reading it back under `provider: local` refused with `EmbedderIdentityMismatchError` naming both encoders |
| Embedder model: bge-small-zh ↔ bge-base-zh ↔ e5-small ↔ MemPalace's two | `embedding.model` | `test_local_embedder`, plus full bench runs at five of them — the palace marker records the configured model each time, which is the check that the earlier runs were missing |
| Graph: none ↔ sqlite | `kg.backend` | `test_kg_optional` + round-trip e2e |
| Model files: hub ↔ local directory | `embedding.model_dir` | `test_local_embedder`; only Eidolon's local provider owns this switch, while MemPalace native providers reject it because 3.8 exposes no public equivalent |
| Shard: which spaces one process serves | `allowed_spaces` | `test_router_contract` |

`mempalace.embedding_model` and its three siblings still configure the embedder and
are folded into the `embedding` section before validation, so a value written at the
old address is validated by the new rules rather than skipping them. Setting the
same thing in both places is refused when the two disagree.

An unrecognised embedder name is refused at config load. That is the one switch
where a typo used to be silent: MemPalace answers a name it does not know with
`minilm`, so the service would start, build the palace, and retrieve badly — which
is how every quality number before 2026-08-03 came to measure the wrong model.

### The switch that was actually exercised

Three full pipeline runs, differing only in the configured embedding model (written
as `mempalace.embedding_model` at the time, `embedding.model` now):

| embedder | correct | p50 | p95 | RSS |
|---|---|---|---|---|
| minilm | 11/49 (22.4%) | — | 92 ms | 381 MB |
| embeddinggemma | 23/49 (46.9%) | 575 ms | 704 ms | 3 GB |
| **bge-small-zh** | 21/49 (42.9%) | **26 ms** | **30 ms** | **133 MB** |

Each run rebuilt its palace from the same 40 turns, and each palace's
`mempalace_embedder.json` recorded the configured model — which is what proves the
registration reached the spawned agent subprocess, not just the test process.

The bge run had 35 fragments stored at query time against embeddinggemma's 39, so
the two-answer gap is measured on four fewer documents and the comparison favours
embeddinggemma. The latency difference is not subject to that.

`abstention` (0/5) and `future_plans` (0/3) came out identical under both, which
the offline probe predicted before either run. They are the two categories no
embedder choice will move.

### What was removed rather than fixed

The cloud implementations: six PostgreSQL ledgers, the PostgreSQL graph, the
stateless router, the Milvus configuration path, the cloud profile, and two
optional dependency groups. About 2000 lines, plus their tests.

The abstraction layer stayed. It is not there for a second implementation — the
router is what makes a space a parameter instead of the process's identity, and
`EmbeddingPort` exists because MemPalace picks its encoder with a hardcoded
if/else and offers no registration hook. Both solve a problem that exists today.

### Opening data written by an older version

Three ledgers gained a `memory_space_id` column, and a file from before it opens
fine then fails on the first statement — inside a constructor, for
`command_status`, so the space never resolves and the agent does not start.

This was found by checking the four live palaces on this machine, not by a test:
an e2e palace is always freshly created, so no suite can see it. Four tests now
pin the behaviour, and it was verified against copies of the real files.

The guard sorts by what the rows are worth. An empty table is rebuilt. A
populated `command_status` is rebuilt, because it is a projection whose
documented failure mode is a lost final status showing a command as `accepted`
again — never an unapplied one as successful. A populated dead-letter or sync
table refuses and names the file, because those are failed turns worth inspecting
and the record that stops a device replaying itself.

### PostgreSQL testing

`pgserver` ships PostgreSQL binaries as a wheel, so the shared-storage suites run
a real server with no container and no system package. Before this, the PG paths
were verified only structurally and the 11 tests that would have proved otherwise
were gated behind an environment variable nobody set.

Point `EIDOLON_MEMORY_PG_TEST_DSN` at another server to check a specific version.
Each test gets its own schema, dropped afterwards.

---

## Contract tests

**133 passed.** Two groups.

### The distributable contracts package (46)

```bash
cd contracts && uv run --isolated --with pydantic --with pytest --with pytest-asyncio pytest tests -q
```

Run with **only pydantic installed** — no MemPalace, no chromadb, no onnxruntime.
That is the point: a client speaking the protocol must not need the service's
storage stack. The isolated run is the proof.

### In-repo contract and boundary tests (87)

| Suite | Tests | Enforces |
|---|---|---|
| `test_kg_sqlite` | 41 | The graph port's behaviour |
| `test_backend_contract` | 14 | Vector port, and the 5-field hot-path minimum |
| `test_layering` | 10 | Every ledger satisfies its port; the logic layer imports no storage library and compares no backend name |
| `test_backend_capabilities` | 16 | Warming and room enumeration are optional, correct when absent, and **survive the LockedBackend wrapper** |
| `test_os_import_boundary` | 4 | Core imports no `eidolon_*` package — static scan plus a subprocess load with OS packages blocked |
| `test_lazy_import_guard` | 2 | No unjustified deferred imports |

The two boundary suites were sabotage-verified: a violation was injected
temporarily to confirm each actually fails.

#### What this category missed, and now covers

The capability tests originally exercised only bare adapters. Production never
holds one — the router wraps every store in `LockedBackend`, which forwards each
method by hand and so answered "no" to a capability added after it was written.
Warmup silently stopped running, and because warming is best-effort by contract
that raised nothing. It surfaced only in e2e, as recall's graph lookup exceeding
its 300ms budget while the embedding model loaded on the first request.

Five tests now cover the wrapped case. The general rule this produced: a new
capability protocol must be tested **through the wrapper production actually
uses**, because decorator plus capability discovery is a silent-failure surface.

### The abstraction layer, as measured

25 protocols in `domain/`. Every one has a consumer, which is worth stating
because an unused protocol is a layer that looks like a boundary and enforces
nothing:

| Group | Protocols | Where they are declared as parameters |
|---|---|---|
| Vector | 6 + `VectorStorePort` | `VectorStorePort` is a deliberate second name — `MemoryBackend` says where an implementation sits, this says what it does |
| Capabilities | `WarmableBackend`, `RoomGraphBackend` | Startup and the graph tool, checked with `isinstance` |
| Graph | `KnowledgeGraphPort` | One implementation, plus an off switch |
| Ledgers | 6 × Reader/Writer/Store | **Read/write separation is used**: `mcp_server` takes `CanonicalFactReader`/`CommitmentReader`, `turn_processor` takes four `*Writer`. Each consumer declares the smallest surface it needs |
| Routing | `MemorySpaceRouter` | `LocalPalaceRouter` (the handle pool) and `FixedSpaceRouter` (a wrapper for handles opened elsewhere) |

`DlqReader` has no direct consumer but composes `DlqStore`, so removing it would
leave `DlqStore` undefinable — structural, not empty.

The protocols are not the gap in this dimension. Read/write separation is used
properly: `mcp_server.py` declares `CanonicalFactReader` and `CommitmentReader`
for its read surface, `turn_processor.py` declares the writer halves, and each
consumer names the smallest face it needs.

### The gap in this category

`MemoryReadContract` and `MemoryWriteContract` are defined and tested, but
`MemoryReadContract` is now implemented — all eight methods are on
`MemoryService`, and the service *is* the contract rather than something adapted
to it. What is still missing is the other side: the agent defines its own
`MemoryRecallResult` and consumes MCP tool JSON, so the read path has no typed
contract *in practice*.

What is actually load-bearing today is the wire layer — subjects, envelope,
payload, and `MemoryActorContext` — of which the agent uses five symbols, all on
the write path.

Closing that is a two-repository change, not a one-repository one:
`RecallResult` deliberately omits `kg_triples`, and the agent reads it in
seventeen places. Narrowing the response means both sides ship together.
Every read method does already have a matching MCP tool, under a different name.

| Contract method | MCP tool |
|---|---|
| `recall_context` | `eidolon_memory_recall_context` |
| `search` | `eidolon_memory_search` |
| `read_active_commitments` | `eidolon_memory_commitments` |
| `get_by_source_turn` | `eidolon_memory_get_by_source_turn` |
| `preview_forget` | `eidolon_memory_forget_preview` |
| `command_status` | `eidolon_memory_command_status` |
| `health` | `eidolon_memory_status` |

The three write methods map the same way: `publish_turn` to the NATS turn
subject, `write_confirmed_fact` to `eidolon_memory_user_confirm`,
`confirm_forget` to `eidolon_memory_forget_confirm`.

So the contract is not a competing design — it is the same surface with a typed
signature and a name per operation.

**But making it the surface in use is a cross-repository change, not a local
one.** `RecallResult` deliberately has no raw `kg_triples`; the Agent MCP
surface exposes projection evidence separately and no longer carries a second
process-local conversation transcript. The tool returns graph evidence today,
and `eidolon_agent/infra/memory/port_adapter.py` consumes it — 17
references on the agent side.

So implementing the contract inside this repository alone would produce exactly
the thing to avoid: the contract returning one shape while the tool returns
another. Unifying means narrowing the response, which memory and agent have to
ship together.

What *is* local, and is the right first step: `MemoryService` resolving a space
from `ctx` through the router, which makes it multi-space by construction while
MCP tools still hand it a single one. That is the same work as parameterising the
tools by request — only 2 of 27 take a caller context today.

---

## Standing test properties

- **No test is skipped for a missing dependency it could provide itself.** A
  skipped suite reads as a passing one. This is why the PostgreSQL suites became
  dev dependencies rather than extras while they existed, and why the two skips
  that remain are named in the report rather than left as a count.
- **Behaviour is asserted against ports, not files.** The 53 ledger tests never
  touch SQLite. That began as a way to run one suite against two storages; it is
  kept because a state machine expressed in SQL statements cannot be tested
  without a database.
- **Guard tests are sabotage-verified.** A guard that cannot fail is decoration.
