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

1. **An ingestion run that completes at all.** Measured at 90+ seconds per turn
   against the configured steward endpoint, so this is the first constraint, not
   a tuning step — see the correction below.
2. **Coverage probe** (~20 sessions), read from the per-stage counters. Only then
   is the R@5 ceiling knowable, and only then does fixing extraction have a
   target.
3. **Session-id mapping** in the ingestion harness, so a hit can be scored the way
   they score it.
4. Then LongMemEval dev (50), then held-out (450), then LoCoMo, ConvoMem,
   MemBench.

### Probe result — and a correction, 2026-08-03

**The 2026-08-02 reading of this probe was wrong.** It is corrected here rather
than edited away, because the mistake is the more useful half.

What the probe reported:

```
Corpus turns published: 40
Palace state at query time: fragments=7, entities=7, triples_total=5
Ingestion wait time: 362.0s
```

I read that as extraction coverage of 17.5% and concluded that extraction quality
capped R@5. That inference skipped a step: **it assumed all 40 turns had been
processed.** They had not.

Re-running with per-turn counters showed what actually happened:

```
turn_processor_decision_persisted: 7      ← of 40 turns published
llm_steward_fallback_to_rules: 2
  litellm.Timeout: timeout value=30.0, time taken=93.44s
```

The steward's LLM takes **90+ seconds per turn** against the configured endpoint,
with a 30s timeout. `--ingest-timeout` defaults to 360s, and the run used 362 —
so it **timed out**. Forty turns at that rate need roughly an hour. The probe then
proceeded to query a palace holding about a sixth of the corpus.

So the number measured **the wait budget, not the memory**. Nothing is yet known
about extraction quality on this corpus.

Two things were wrong, and both are fixed:

* The bench printed a warning to stderr and carried on. That warning never
  reached the report, so the figures below it looked like measurements of a fully
  ingested corpus. It now **exits non-zero** unless `--allow-partial-ingestion`
  is passed explicitly.
* I drew a conclusion from an aggregate without checking that its input was
  complete. The per-stage counters (`FRAGMENTS_EXTRACTED`) exist now so the next
  reading cannot make the same mistake: they distinguish "the model proposed
  little" from "the thresholds discarded it" from "the turn was never processed".

### What the two runs do establish

| | Run 1 (08-02) | Run 2 (08-03) |
|---|---|---|
| Fully correct | 12/49 (24.5%) | 10/49 (20.4%) |
| Latency p95 | 104.6ms | 110.0ms |
| Turns actually ingested | unknown | **7 of 40** |

Latency is real and fine — those queries ran against a live service. The accuracy
figures are not usable, and the variance between them is consistent with both runs
having ingested a different arbitrary fraction.

`preference 0/4` and `abstention 0/5` appeared in both runs, which is suggestive
but not evidence: the relevant memories may simply never have been written.

### What has to happen before any benchmark number means anything

1. **An ingestion run that completes.** Either a faster steward endpoint or a
   timeout sized to the real per-turn cost. At 90s/turn, LongMemEval's 500 long
   sessions are not merely slow, they are impractical — so this is a
   prerequisite, not a tuning step.
2. **Then** re-read coverage from the counters, and only then judge extraction.

prompt-and-evaluation problem whose depth this probe does not measure.

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
