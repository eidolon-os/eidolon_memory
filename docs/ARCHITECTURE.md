# Architecture, and what is actually built

Written 2026-08-03. Every number here was measured on this branch, not recalled.

Read this before the code. It answers two questions: what shape the service has,
and how far each part of it got.

---

## The one idea

**A memory space is a parameter, not the identity of a process.**

Everything else follows from that. The service used to *be* one space: handles
resolved at startup, one port derived from the space id, one advisory lock. That
made the resident embedding model a per-space cost and made "one owner, three
companions" cost three processes and three copies of a 300MB model.

Now a caller passes its context, the router resolves that space's handles, and
the service answers. How many spaces a process holds is a deployment decision.

---

## Layers

| Layer | Files | Lines | Owns | May not |
|---|---|---|---|---|
| `contracts/` (separate package) | 12 | 1230 | The wire and service contract. Depends only on pydantic | know anything about storage |
| `domain/` | 20 | 2552 | Ports, models, pure decisions | do I/O |
| `application/` | 26 | 5937 | Recall, turn processing, the service object | import a storage library, or compare a backend's name |
| `adapters/` | 15 | 4172 | Vector store, graph, routers | — |
| `infrastructure/` | 22 | 6126 | Ledgers, NATS, palace bootstrap | — |
| `entrypoints/` | 7 | 3692 | Process wiring: MCP, subscriber, supervisor | hold business logic |
| `config/` | 6 | 1158 | Settings, registry | import adapters |
| `support/` | 5 | 334 | Metrics, tracing, logging | — |

Two of these boundaries are enforced by tests rather than convention
(`test_layering.py`): the logic layer may not import mempalace, and may not
compare against a backend name. Both caught real violations when written.

---

## Ports and their implementations

Every port lives in `domain/`; every implementation is named after the thing it
talks to. 25 protocols, all with a consumer.

```
MemorySpaceRouter ─── LocalPalaceRouter      pool of embedded handles, one flock per space
                  ├── SharedStoreRouter      stateless, any replica serves any space
                  └── FixedSpaceRouter       handles opened elsewhere, one space

VectorStorePort ───── MemPalacePythonBackend  the only real one; chroma↔milvus
                  │                           switches inside mempalace via env
                  ├── LockedBackend           wrapper adding the per-space lock
                  └── FakeMemoryBackend       tests

KnowledgeGraphPort ── SqliteKnowledgeGraph   ┐ share kg_sql.py: schema and
                  └── PostgresKnowledgeGraph ┘ query shapes, one source

WarmableBackend      capability — a store says whether it can be warmed
RoomGraphBackend     capability — a store says whether it can list rooms

6 × ledger ports ──── SQLite (in palace)     ┐ share ledger_sql.py
                  └── PostgreSQL (shared)    ┘ 5 of 6 implemented
```

**Asymmetry worth knowing:** the graph and the ledgers have two implementations we
own. The vector store has one — chroma↔milvus switching happens *inside*
mempalace. So if the milvus path misbehaves we can verify but not fix it. That is
a consequence of mempalace having a real backend registry for vectors and none
for graphs; it is not a choice we made.

---

## The read path, end to end

```
agent ──MCP──▶ mcp_server tool ──▶ MemoryService.recall_fused(ctx, …)
                                        │
                                        ├─ router.resolve(ctx.memory_realm_id)
                                        │     └─▶ backend / kg / ledgers for that space
                                        │
                                        └─ recall_with_kg_fusion
                                              ├─ vector search across wings
                                              ├─ graph lookup (bounded, optional)
                                              ├─ theme channel
                                              └─ recall policy: space, device, audience
```

Two of the 27 MCP tools go through the service today: `search` and
`recall_context` — the only two the agent reads through. The other 25 are
operator-facing and still single-space, which suits their semantics.

`search` and `recall_context` answer different questions and are deliberately not
the same path: search is "what do you remember about this", recall is "what is
relevant to this turn" (hence graph fusion, recent turns, session filtering).

---

## Two-layer visibility

```
audience = "owner"           facts about the owner — every companion may recall
audience = "companion:<id>"  what happened with that one — private to it
```

Enforced in the graph's queries and in the vector store's visibility gate, both
defaulting to the owner layer. Provenance (`companion_id`) is a separate field
from visibility (`audience`) — conflating them would make every memory private by
accident.

**This axis is currently inert in production**, and the reason is structural: a
space is `(owner, companion)` today, so each companion is a separate store.
Nothing to leak, and nothing to share. It becomes real when a space is an owner,
which is what the 1:N work below enables.

---

## Deployment shapes

One codebase. The shape is *derived from where storage lives* — there is no
`mode: local|cloud` flag, because a flag could contradict the storage config.

| | Local | Cloud |
|---|---|---|
| Vector | chroma, file in palace | milvus server |
| Graph | SQLite in palace | PostgreSQL |
| Ledgers | SQLite in palace | PostgreSQL (5 of 6) |
| Router | `LocalPalaceRouter` — flock per space | `SharedStoreRouter` — no lock, no port derivation |
| Turn ring | in-process | absent (would differ per replica) |
| Connection pool | n/a | **one per replica**, shared by graph and every ledger of every space |
| Process : space | **1 : 1 today**, 1 : N once the last steps land | M replicas : all spaces |

The single asymmetry, asserted by test: embedded storage refuses a second holder;
shared storage serves one space from two replicas concurrently.

---

## Progress

### Done and verified

| | Evidence |
|---|---|
| Standalone from Eidolon OS | Core code imports no `eidolon_*` package — enforced by two guard tests (static AST scan, plus a subprocess that loads every entrypoint with OS packages blocked). Contracts' 46 tests pass with only pydantic installed. **Precisely:** the one `eidolon-*` in core dependencies is `eidolon-memory-contracts`, which is this repository's own package (`path = "./contracts"`, pydantic-only); `eidolon-data` appears only in the optional `eidolon-os` extra and in dev |
| Contract package self-hosted | 12 files, 1230 lines, pydantic-only |
| `MemoryReadContract` implemented | All 8 methods on `MemoryService`; the contract *is* the service |
| Vector chroma ↔ milvus by config | Verified against the live instance (8.140.214.42, `eidolon` db) |
| Own graph, two dialects | 41 SQLite tests + 11 against a real PostgreSQL |
| PostgreSQL testable at all | `pgserver` ships the binaries as a wheel; 11 tests that had never run now run in the ordinary suite |
| Ledgers on shared storage | **5 of 6** — decisions, sync, dlq, command_status, commitments |
| One state machine, not two | Commitment decisions extracted as a pure function both storages call; source-checked so a copy cannot reappear |
| Ledger writes bounded | Per-ledger serialisation in the event loop, plus a process-wide ceiling below the thread pool size |
| Two-layer visibility | Enforced in graph queries and the vector visibility gate |
| Observability | prometheus `/metrics` on the existing port; contextvar spans with OTel field names |

**900 unit/contract tests, 6 skipped. e2e 25 passed / 2 failed** (the two are a
pre-existing LLM extraction shortfall — see TEST_REPORT.md).

### Not done, with the actual blocker

| | State | Blocker |
|---|---|---|
| `canonical_facts` on PostgreSQL | **The one missing ledger.** Schema is already shared; the SQLite side works | 12 methods, ~1000 lines to translate. Mechanical now, but not small |
| Process : space = 1 : N | Router, service and ledger bounds all support it. `agent_runner.py` still passes `allowed_spaces=[one]` | 3 handlers take fixed handles and must take the router — **47 test call sites**, mixed formats, not safely scriptable |
| NATS one consumer for all spaces | Wildcard subject helpers already exist; `turn_processor` already reads the space from the payload | Same 47 call sites |
| Single endpoint / discovery | | supervisor owns process topology — **deferred at your instruction** |
| Narrow the MCP response | `RecallResult` omits `kg_triples` by design | agent's `port_adapter.py:201` reads it — needs both repos in one batch |
| Write-side audience layering | Everything is the owner layer | needs the steward to judge per statement |
| Four public benchmarks | Method aligned, probe run, harness planned | **extraction coverage: 7 fragments from 40 turns.** R@5 cannot exceed that, so a number today would measure a known defect |

### What "cloud is missing a ledger" costs concretely

`SharedStoreRouter` hands back `None` for `canonical_facts`, and every consumer's
existing `None` handling keeps the service running. So a shared-storage deployment
starts, serves recall, and **does not invalidate a corrected fact** — the chain
that makes a superseded memory stop being recalled lives in that ledger.

This is stated in the router's startup log and in
`config/settings.cloud.example.yaml`, so an operator meets it before a user does.

---

## The single most important open number

**Extraction coverage: 7 fragments from 40 turns (17.5%).**

MemPalace stores every session verbatim, so its published 96.6% R@5 measures
retrieval over everything. Ours would measure retrieval over the sixth of the
corpus the steward chose to keep. Category detail from the same probe: emotion
3/3, time 3/5, **preference 0/4**, **abstention 0/5**.

Preference is the central thing a companion memory answers. Latency is not the
problem — p95 104ms end to end through MCP.

Improving the retriever would barely move any of this. That is why the benchmark
order is: fix extraction, rerun the probe, then publish numbers.
