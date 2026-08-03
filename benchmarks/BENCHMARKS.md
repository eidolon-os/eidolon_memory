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

### First complete run, 2026-08-03 — and four corrections

This is the first probe run whose input was complete, so it is the first figure
worth citing. Getting here took fixing three measurement defects and abandoning a
conclusion, all recorded below because each was plausible and wrong.

```
Corpus turns published:  40
Turns fully processed:   40        ← the first run where this held
Palace at query time:    fragments=39, entities=32, triples_total=31
Ingestion wait:          1192.7s
Threshold filtering:     0 fragments dropped
Queries:                 11/49 correct (22.4%)
Latency:                 p50 73.8ms, p95 92.4ms
```

**Extraction is not the constraint.** 40 turns produced 39 fragments — roughly
97.5% coverage, and not one fragment was discarded by either threshold
(`max_fragments_per_turn`, `min_importance_to_write`). The memories are being
written.

**Retrieval is.** With the corpus fully present, 11 of 49 queries are answered
correctly:

| Category | Correct | Evidence recall | Omissions | Reading |
|---|---|---|---|---|
| canonical_entity | 3/7 | 64.3% | 5 | Finds most of the evidence, still answers wrong |
| emotion | 2/3 | 66.7% | 1 | Works |
| kinship_alias | 3/8 | 50.0% | 8 | Half the evidence, a third of the answers |
| time | 2/5 | 40.0% | 3 | |
| preference | 1/4 | 20.0% | 4 | The central companion-memory case |
| **topic** | **0/8** | 21.4% | **11** | Most omissions of any category |
| **future_plans** | **0/3** | **0.0%** | 6 | Retrieves nothing at all |
| **event** | **0/3** | 20.0% | 4 | |
| **pronoun** | **0/3** | 33.3% | 4 | |
| **abstention** | **0/5** | — | 0 | **A separate failure — see below** |

**Evidence recall exceeds the correct rate almost everywhere.** That shape matters:
it means recall is finding *some* of the material a question needs and not the
rest. This is a completeness and ranking problem inside recall, not a storage
problem — which is the opposite of what the earlier readings suggested.

**Abstention is a different defect.** Those five questions are unanswerable from
the corpus, and the correct behaviour is to decline. Zero clean abstentions with
zero omissions means it answered all five. That misleads a user rather than
disappointing them, so it is worse than a miss despite costing the same in the
aggregate.

**Latency is fine and always was**: p95 92ms end to end through MCP, across four
runs (66–110ms).

### The four things that were wrong before this run

Recorded rather than edited away — each was believable, and the sequence is the
useful part.

1. **The steward timeout was tighter than the work.** Measured: a trivial call to
   this endpoint returns in 2.4s, the real 8.3k-char steward prompt takes 22.9s,
   and the limit was 30s (code default 20s). Ordinary variance tripped it and
   litellm's retries turned one slow turn into ~93s. Now 90s.
2. **The bench's ingest budget could not fit the corpus.** Turns are processed
   strictly one at a time, so 40 of them need ~15 minutes; the default was 360s.
   Now 1800s — the first complete run used 1192.7s, i.e. the 1200s I set after
   the first measurement left seven seconds of margin.
3. **The drain gate measured output, not arrival.** It waited for `triples >= 18
   and fragments >= 25`, which saturated at 24 of 40 turns — so the bench decided
   it was finished and queried a palace missing 40% of the corpus. It now waits
   for the turn consumer's backlog to reach zero, which is what "ingested"
   means. Thresholds remain as a floor beneath that, since a drained queue with
   no output means extraction is broken.
4. **And the conclusion I drew from all of it.** From "40 published, 7 fragments"
   I wrote that extraction coverage was 17.5% and capped R@5. The real coverage
   is 97.5%. I read an aggregate without checking its input was complete, three
   times, and each measurement defect made the next inference look better
   supported than it was.

`FRAGMENTS_EXTRACTED` now separates "the model proposed little" from "the
thresholds discarded it" from "the turn was never processed" — the third being
what fooled me — and the bench exits non-zero rather than reporting on a partial
corpus.

### What to do next, in this order

1. **Recall completeness**, starting with the four categories at 0%. `topic` has
   the most omissions of any category and `future_plans` retrieves nothing, so
   they are the two with the most to learn from.
2. **Abstention**, separately: answering an unanswerable question is a different
   bug from missing a memory.
3. Only then the public suites. At ~30s per turn of ingestion, LongMemEval's 500
   long sessions remain a scale problem regardless of quality.

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
