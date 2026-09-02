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

### The prompt change, measured — 2026-09-01

The steward prompt was shortened to stop asking for fields the service
overwrites, and `evidence_quote` was added because the two prompts disagreed
about it.  That is a change to what the model is asked for, so it was carried as
the one unmeasured thing in the release.  It is measured now, by a controlled
A/B on `scripts/benchmark/bench_memory_retrieve_quality.py` — same corpus, same
query battery, same retriever, one variable.

| | correct | fragments | active triples | steward failures |
|---|---|---|---|---|
| Old prompt (`a490f8b`) | 18/48 — 37.5% | 39 | 20 | 5 / 35 ingests |
| New prompt (`9f0ac36`) | **20/48 — 41.7%** | 41 | 25 | 4 / 46 ingests |

**The claim this supports is "no regression", not "+2".**  The two runs did not
ingest identical material — five discarded turns against four — so the
difference is inside what one run each can resolve.  What it does establish is
that the new prompt is not worse on any axis measured: it extracted more
material and scored no lower.  Recall latency was p95 16.8ms against 16.7ms.

Neither number is comparable to the 21/49 in `ARCHITECTURE.md`.  That was the
pre-3.8 stack with an in-process embedder and a 49-query battery; this is 3.8
storage through the openai-compat provider against 48.

#### Why this had not been measured before

The bench refused to report.  `require_expected_embedder` compared the palace's
recorded embedder against `embedding.model` — `bge-small-zh` — but 3.8 storage
takes vectors through the public openai-compat provider, so MemPalace records
`openai-compat` whatever model answers the endpoint, and `provider: local` is
refused outright by the 3.8 backend.  No supported configuration could satisfy
the comparison, so every 3.8 run exited 2 and extraction quality stayed
"unknown" rather than measured.

The expectation is now derived from `mempalace_backend_env`, which is the one
place the provider-to-recorded-name mapping lives, and the marker is read
through `palace_inventory.palace_embedder` — whose own docstring says a second
copy of it is how two callers come to disagree about one palace.  This gate was
that second copy.  The guard keeps its purpose: a palace built offline still
records `minilm`, still mismatches, still refuses.

#### A fourth instance of the closed class, and closing it

Both runs were dominated by the same failure, and it is not the prompt's:
`MemoryIntent.occurred_at` blank fails validation and discards the whole
decision — 4 of 4 failures in the new-prompt run, 4 of 5 in the old.  The field
is `str | None`, and the turn processor fills it with the turn's own timestamp
when it arrives as `None`.  So one field had two spellings of "I have no
timestamp", one repaired and one fatal.

Fixed in `34e4aae`, and re-measured on the same corpus and battery:

| | correct | ingest wait | steward failures | `occurred_at` | deliveries for 40 turns | fragments | active triples |
|---|---|---|---|---|---|---|---|
| Old prompt | 18/48 | 1611s | 5 | 4 | 45 | 39 | 20 |
| New prompt | 20/48 | 1974s | 4 | 4 | 46 | 41 | 25 |
| New prompt + fix | 19/48 | 1755s | **1** | **0** | **42** | 38 | 18 |

The class is closed: the one failure left is the model returning something that
is not JSON at all, which is a model fault and not a validation one.

**The gain is write cost, and it is not a quality gain.**  Every discarded
decision was retried and the retry succeeded, so the memory was delayed rather
than lost — which is exactly what the Pi log showed for the 118.6s round.
Removing the retries removes wasted model calls (five or six extra deliveries
per 40 turns, down to two) and removes the worst-case per-turn latency; it does
not add material.  The prediction that it would improve extraction was wrong.

#### What three runs say about the benchmark itself

18, 20 and 19 of 48.  `ARCHITECTURE.md` records zero run-to-run variance for
this harness — 21/49 twice with no per-query flips — but that was the pre-3.8
stack.  On 3.8 the spread is ±2, so **no single-run comparison in this session
could resolve a two-query difference**, including the prompt A/B above.  Read
latency was the stable measurement throughout: p95 16.7ms, 16.8ms, 16.8ms.

Using this as a gate needs repeated runs per configuration, not one.

### verbatim 与蒸馏 — 2026-09-02，本地测量

一个问题，其余全部固定：**把 40 轮语料原样入库、全程不用 LLM，同一套查询的 top-5 召回能不能
拿到每条标注要求的证据串？**

可比是因为它走同一个 `recall_with_kg_fusion`、同一个 `top_k`、同一个 embedder，并套用基准
scorer 同一个子串判定。指标是 `vector_hit` 而不是基准的 `correct`：`correct` 把缺失的 KG 证据
组算作失败，用它给一个无图的 palace 打分等于回答一个没人问的问题。verbatim 侧全部落在同一个
wing/room，按语料自带的 `wing_hint` 路由会白送它蒸馏侧必须自己挣的信息。

| | 43 条可答查询命中 | LLM 调用 | 可读时间 |
|---|---|---|---|
| verbatim，仅用户原文 | **32（74.4%）** | **0** | 毫秒 |
| verbatim，user + assistant | 31（72.1%） | 0 | 毫秒 |
| 蒸馏 A / B / C | 28 / 28 / 25（58–65%） | 每 40 轮 40 次 | 每轮 17–75s |
| 仅用户原文 ∪ 单次蒸馏 | **36–37（84–86%）** | | |

**只存用户原文比连 assistant 一起存更好**，31 → 32。助手的复述不带新信息，只稀释嵌入并挤占
top-5 名额。这给 `test_steward_prompt_does_not_send_assistant_text` 那条既有规则补了一个检索
侧的理由，而不只是「别把模型散文当第二个事实源」。

**两者互补，各有 6 条是对方拿不到的**（verbatim 用仅用户原文，蒸馏取三次并集）：

- 仅 verbatim：`王芳呢`、`我老婆和我吵架`、`它身体怎么样`、`我有什么爱好`、`我有什么健康问题`、
  `我和宠物的事` —— 代词、模糊、宽泛类。原话在库里，所以能中。
- 仅蒸馏：`我什么时候开心`、`我去医院的事`、`我答应了妈妈什么`、`我计划去哪里`、
  `我答应了什么事情`、`最近开心的事` —— **抽象**类。原始对话里从来没有「我答应了…」这种措辞，
  是 steward 写出了那个句子。

读法：**verbatim 答「说过什么」，蒸馏答「这意味着什么」。** 41.7% 的整例正确率因此不是检索
得分，是一次性抽取的天花板——换 embedder 或加 rerank 都动不了那 6 条，它们不在库里。

#### 有效性威胁，按重要性

1. **规模未验证。** 40 轮的 palace 很小。verbatim 每轮一条、随对话线性增长，蒸馏事实有界
   （40 轮 → 38 个抽屉）。一万轮时的检索表现没有数据。MemPalace 自带 `dialect.py`（AAAK 压缩）
   和 `dedup.py` 两个零 API 模块看起来是为这个问题准备的，本仓都没用过也没测过。
2. **只测了证据可检索性。** 蒸馏还产出 KG 事实、canonical 失效链、commitment 生命周期，
   verbatim 一个都不给。这些是产品核心，不在这个指标里。
3. **两次 verbatim 运行是确定性的**（无 LLM），32 可复现；蒸馏侧有 ±2 方差，所以「仅蒸馏 6 条」
   取的是三次并集而不是单次。

复现脚本没有进仓——它建临时 palace、写 40 条、跑 48 查询，属于一次性测量而不是门禁。要重跑
就照上面的口径重写：同一个 `recall_with_kg_fusion`、`top_k=5`、`kg=None`、单一 wing/room、
仅 `user_text`。

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
| `test_kg_sqlite` | 81 | The graph port's behaviour |
| `test_backend_contract` | 15 | Vector port, and the 5-field hot-path minimum |
| `test_layering` | 13 | Every ledger satisfies its port; the logic layer imports no storage library and compares no backend name |
| `test_backend_capabilities` | 16 | Warming and room enumeration are optional, correct when absent, and **survive the LockedBackend wrapper** |
| `test_os_import_boundary` | 3 | Core imports no `eidolon_*` package — static scan plus a subprocess load with OS packages blocked |
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

26 protocols in `domain/` (counted 2026-09-02). Every one has a consumer, which is worth stating
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
