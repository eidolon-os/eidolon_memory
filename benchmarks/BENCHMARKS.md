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

### Aligning the measure — read before running any of these

Read from MemPalace's published benchmark document rather than assumed, because
running the same datasets under a different definition would produce numbers that
look comparable and are not.

**Their unit of retrieval is a whole session.** The baseline "stores every session
verbatim as a single document", and recall asks: *"is the labelled session for this
question inside the top-5 retrieved candidates?"* No LLM extraction runs at
ingestion time.

**Ours is a fragment.** A session becomes however many memory fragments the
steward decides to write — possibly none. So a like-for-like comparison needs a
mapping: a hit is *any* retrieved fragment whose source session is the labelled
one. That requires fragments to carry their source session id through ingestion,
which is a change to the harness, not to the service.

**And that mapping exposes a ceiling we can measure before running anything.** A
session the steward declines to write produces no fragment and is therefore
unreachable at any k. **Extraction coverage is the hard upper bound on R@5.** The
e2e suite currently shows the steward extracting 1–3 triples where a 12-turn
conversation should yield ≥8, so this bound is not hypothetical.

The cheap probe is therefore: ingest ~20 sessions, measure what fraction produced
at least one fragment. If coverage is 60%, R@5 cannot exceed 60% and the honest
next step is fixing extraction, not running 500 questions to publish a number that
measures a known defect.

### Practical constraints, confirmed

| Item | Finding |
|---|---|
| Dataset | 3.04 GB. The original `longmemeval` is **deprecated** in favour of `longmemeval-cleaned`, which removes noisy history sessions |
| Which file | MemPalace runs `longmemeval_s_cleaned.json` — the comparison must use the same one |
| Split | They publish `benchmarks/lme_split_50_450.json` (50 dev, 450 held-out), so the split can be reproduced exactly rather than approximated |
| Their harness | `benchmarks/longmemeval_bench.py`, with `--mode`, `--held-out`, `--split-file` flags |
| Ingestion cost | Theirs is embedding-only. Ours runs an LLM steward per session, so ingesting 500 long sessions is the dominant cost and the reason a probe comes first |

### Order of work, and why

1. **Coverage probe** (~20 sessions). Establishes the R@5 ceiling. Cheap, and it
   decides whether the rest is worth running.
2. **Fix extraction** if coverage is the binding constraint.
3. **Session-id mapping** in the ingestion harness, so a hit can be scored the way
   they score it.
4. Then LongMemEval dev (50), then held-out (450), then LoCoMo, ConvoMem,
   MemBench.

No date is given for the full report because the second step is an LLM quality
problem with an unknown depth. The probe is what turns that into an estimate.

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
