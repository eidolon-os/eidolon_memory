# Benchmarks

Status: the harness is in place and the latency numbers below are measured. The
quality numbers are **not yet run** — the table states the targets and what they
are being compared against, with empty result columns, because publishing a
placeholder as a result is how benchmark pages become untrustworthy.

Every figure here comes with a manifest recording the commit, machine, embedder
and backend that produced it (`benchmarks/README.md` explains why each matters).

---

## Latency

The measurements a conversational memory has to answer for, since retrieval sits
on the critical path of a reply.

**Budgets we hold ourselves to**, from what a reply can absorb rather than from
what is convenient:

| Path | p50 | p95 | Where the budget comes from |
|---|---|---|---|
| voice recall | ≤20ms | ≤60ms | Industry guidance puts voice retrieval under 100ms for a sub-800ms reply |
| chat recall | ≤120ms | ≤200ms | 200ms is the commonly cited chat retrieval budget |
| deep / admin | ≤300ms | ≤600ms | Off the reply path; bounded so it cannot starve one |
| write ack | ≤35ms | ≤50ms | JetStream publish acknowledgement |

**Measured** (local, chroma, single machine — see the run manifests for host
details):

| Path | p50 | p95 | p99 |
|---|---|---|---|
| voice recall | 12ms | 34ms | 36ms |
| chat recall | 388ms | 445ms | 567ms |
| chat recall + graph | 392ms | 544ms | 664ms |
| write ack | 31ms | 39ms | 43ms |

Voice sits comfortably inside its budget. **Chat does not** — 445ms against a
200ms target. Three causes are identified in the code rather than guessed at:

1. The literal-scan fallback triggers whenever vector search returns fewer than
   `top_k` results, not only when it fails, and off the voice path it scans up to
   5000 rows.
2. The theme channel runs serially after the parallel vector and graph work
   instead of alongside it.
3. The graph budget off the voice path was a hardcoded 1.0s — longer than the
   entire chat retrieval budget. **Fixed**: now `recall.kg_timeout_seconds_normal`,
   default 0.3s.

The first two are not yet addressed, which is why chat is reported as failing its
target rather than quietly omitted.

---

## Quality — the comparison

MemPalace publishes retrieval recall on public datasets. Those are the numbers to
beat, on the same datasets, at the same k, under the same definition of a hit.

| Dataset / measure | MemPalace | Ours |
|---|---|---|
| LongMemEval 500q, R@5 (raw) | 96.6% | not yet run |
| LongMemEval 450q held-out, R@5 | 98.4% | not yet run |
| LoCoMo 1986q, R@5 / R@10 | 83.7% / 88.9% | not yet run |
| ConvoMem 250, mean recall | 92.9% | not yet run |
| MemBench 8500, R@5 | 80.3% | not yet run |
| MemBench — noisy | **43.4%** | not yet run |
| MemBench — conditional reasoning | **57.3%** | not yet run |
| Retrieval latency p50/p95 | **not published** | measured above |
| End-to-end answer accuracy | not published | not yet run |

Where we expect to differ, and why — stated in advance so the results can
contradict it:

- **Noisy and conditional-reasoning subsets** are MemPalace's weakest results.
  Those are the categories an extraction step should help with: we store what a
  steward judged worth keeping, rather than every turn verbatim, so distractors
  are filtered before retrieval rather than competing during it.
- **Knowledge update** should favour us for a similar reason — a superseded fact
  has its validity interval closed, so it stops being retrievable as current
  rather than ranking below its replacement.
- **Raw retrieval on clean single-session questions** is where verbatim storage is
  strongest and we should expect no advantage.

Two things about method, because they are how such tables usually mislead:

**Retrieval recall is not answer accuracy.** MemPalace's 96.6% is "was the
labelled session in the top 5", not "did the assistant answer correctly".
MemPalace says so explicitly and declines to compare against systems publishing
end-to-end figures — the right call. We report both, labelled, never averaged.

**Tuning splits are not results.** MemPalace's 100% figure is marked as reached by
inspecting three failures, which is why they also publish a 450-question held-out
number. We follow the same practice: tune on a small dev split, publish held-out.

**Same embedder or it proves nothing.** MemPalace's baseline uses
`all-MiniLM-L6-v2`. A comparison run on `embeddinggemma` would measure the
embedder, not the system, so the comparable run uses minilm and the production
configuration is reported separately.

---

## What is not measured yet

Stated so the gaps are visible rather than implied by absence:

- **Quality suites.** Datasets need ingesting and the steward-mode runs cost LLM
  calls. Blocked behind a real defect first: the steward currently extracts far
  fewer graph triples than the pipeline expects (two e2e tests fail on it), so any
  graph-dependent quality number would measure that bug rather than the design.
- **Cloud latency.** The Milvus path is verified functionally against a live
  server but not yet timed, so no cloud figures are claimed.
- **Multi-space concurrency.** One process can now serve several spaces; how p95
  degrades as it does is the number that matters for density and is not yet
  measured.
- **The graph on PostgreSQL.** Implemented and structurally checked against its
  SQLite twin, never run against a server (there is none on the development
  machine).
