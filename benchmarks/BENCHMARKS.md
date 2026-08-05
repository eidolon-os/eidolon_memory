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

## The embedder, and why it was measured separately

The quality suite takes twenty minutes and measures retrieval, extraction, the
steward, the graph and the LLM at once. Choosing an embedder from it costs twenty
minutes per candidate and reads the answer through four other subsystems.

So `benchmarks/suites/probe_embedders.py` asks only what an embedder is
answerable for: given the 39 fragments a real run stored, is the right one in the
top 5 for each of the 49 real queries. Seconds per candidate, and it goes through
the production adapter so pooling and prefixes are the ones that ship.

| model | dim | top-1 | top-5 | RSS | ms/call |
|---|---|---|---|---|---|
| **bge-small-zh** | 512 | 27 → 24 /43 | 36 → 35 /43 | **130 MB** | **1.0** |
| bge-base-zh | 768 | 26 → 24 /43 | 39 → 36 /43 | 130 MB | 3.0 |
| bge-large-zh | 1024 | 24 → 26 /43 | 38 → 36 /43 | 485 MB | 8.6 |
| multilingual-e5-small | 384 | 21 → 24 /43 | 35 → 35 /43 | 180 MB | 1.5 |
| embeddinggemma | 384 | 25/43 | **37/43** | **3 GB** | 44.4 |
| minilm | 384 | 5/43 | 16/43 | 380 MB | 17.5 |

The arrows are the **same model** measured on two corpora — the palaces of two
full runs, holding 35 and 36 stored fragments, against the same 49 queries.

**A single model swings ±3 across those two corpora, which is more than the gap
between the three BGE sizes.** On the first, bge-small has the best top-1 and
bge-large the worst; on the second that inverts. So 43 queries cannot rank the
three sizes, and an earlier version of this table claimed otherwise — it read that
swing as a finding ("bge-base has the best top-1", "bigger is not simply better").
Both were noise.

Three things the two runs *do* establish:

1. **Cost is stable and decisive.** RSS and latency repeat to within a few MB and
   a fraction of a millisecond. bge-large costs ~3.7× the memory and ~8× the
   latency of small for no measurable retrieval gain. So the default is chosen on
   cost: among models that cannot be distinguished, take the cheapest.
2. **embeddinggemma really does retrieve best**, by a margin outside the noise
   band. It is excluded on cost — 3 GB and 44 ms against a 4 GB Raspberry Pi
   shared with other services — not on quality.
3. **minilm's 5/43 is not a tuning gap**, and it is also outside the band. It is
   an English-only encoder being asked Chinese questions, and it was what every
   figure published before 2026-08-03 was measured on, because the bench never
   copied the embedder into its spawn settings and MemPalace applied its own
   default.

### Nine embedders, one process each, two corpora

Every row measured in its own process, because ``ru_maxrss`` is a high-water mark
and a model measured after a larger one reports the larger one's peak — several
earlier figures in this document were wrong for exactly that reason. Latency is the
p95 of 100 calls after 20 warm-up calls; an earlier version reported the max of 20
and called it p95.

| model | dim | RSS | query p50/p95 | top-1 A/B | top-5 A/B |
|---|---|---|---|---|---|
| bge-small-zh | 512 | 130 MB | 0.9 / 1.3 ms | 27 / 24 | 36 / 35 |
| bge-base-zh | 768 | 250 MB | 3.3 / 4.7 ms | 26 / 24 | 39 / 36 |
| bge-large-zh | 1024 | 655 MB | 10.0 / 12.7 ms | 24 / 26 | 38 / 36 |
| bge-m3 | 1024 | 1542 MB | 11.4 / 12.9 ms | 25 / 27 | 38 / 37 |
| **gte-multilingual-base** | 768 | 968 MB | 4.4 / 5.2 ms | **28 / 29** | 35 / 36 |
| multilingual-e5-small | 384 | 180 MB | 1.3 / 1.7 ms | 21 / 24 | 35 / 35 |
| multilingual-e5-base | 768 | 840 MB | 4.0 / 4.8 ms | 26 / 24 | 36 / 36 |
| **multilingual-e5-large** | 1024 | 1541 MB | 15.1 / 18.5 ms | 28 / 27 | **41 / 40** |
| embeddinggemma | 384 | 1532 MB | 50.9 / 59.2 ms | 27 / 27 | 36 / 37 |

**bge-m3 is unremarkable here**, which answers a direct question about it: newer
than the zh-v1.5 line by four months and BAAI's current general recommendation, but
25–27 top-1 and 37–38 top-5 sit inside the band every other model occupies, for
6× the memory and 3× the latency of bge-base.

Two rows do stand outside the ±3 swing that this document has repeatedly warned
about. `multilingual-e5-large` scores 41 and 40 top-5 where every other model scores
35–39 — its *minimum* exceeds most models' *maximum*, which no earlier comparison
here achieved. `gte-multilingual-base` scores 28 and 29 top-1, the highest of the
nine and stable across corpora, at 5.2 ms.

**Neither is recommended on that basis, because the probe has already been shown
not to predict the end-to-end result.** bge-base beats bge-small on probe top-5
(39/36 against 36/35) and loses to it end-to-end (20/49 against 21/49). So the
probe ranks retrieval in isolation and the pipeline does not inherit the ranking.

### How stable the end-to-end metric is, and for which model

| model | runs | correct | per-query flips between runs |
|---|---|---|---|
| bge-small-zh | 2 | 21, 21 | **0** |
| multilingual-e5-large | 2 | 20, **21** | **5** |

The corpora differ almost completely between any two runs — the steward is an LLM
and paraphrases freely, so every pair in this document overlaps by 3–9% of
fragments (Jaccard 0.03–0.09). "咖啡因会引发用户心慌" one run, "咖啡因会让我心慌"
the next.

bge-small-zh came through that unchanged: two runs sharing 2 of 35 fragments, and
not one of the 49 queries changed verdict. That is a real property and it is worth
knowing.

**An earlier version of this section drew the wrong conclusion from it.** It said the
metric is invariant to corpus paraphrase, therefore a one-answer gap between two
embedders is attributable to the embedder. Zero flips for *one* model does not
establish stability for all of them, and e5-large refutes it: its two runs flipped
five queries and moved 20 → 21. So the gap that argument was built on does not
exist — e5-large ties bge-small.

Two consequences:

* **The four deployable models are indistinguishable on quality: 20–21.** No
  measurement here separates them.
* **embeddinggemma's 23 rests on a single run**, and single runs are now known to
  move by ±1 on this metric for at least one model. It should not be read as "+2"
  until it has a second.

The decision therefore does not depend on any quality difference being real, which
is what makes it robust: among models whose quality cannot be separated, the choice
is cost, and cost is the part that repeats to within a few percent.

### Why the probe's top-5 misled

The two metrics ask different questions:

* probe top-5 — did **at least one** expected substring reach the top 5 of the
  vector channel?
* end-to-end `correct` — did **every** expected evidence group produce a hit, across
  the vector channel *and* the graph?

A model can be better at putting one piece of evidence in the top 5 and no better at
putting all of them there, and the graph channel does not involve the embedder at
all. multilingual-e5-large is the clean example: 41/43 on the probe against bge-small's
36 — a five-query lead, the only gap in this comparison that stood outside the
corpus swing — and 20–21/49 end to end against bge-small's 21. The probe's clearest
signal produced no end-to-end difference at all.

### The machine these numbers come from, and the one they are for

Measured on a 12-core Apple Silicon laptop with ``OMP_NUM_THREADS=4``. **The target
is a Raspberry Pi 5**: four Cortex-A76 cores at 2.4 GHz, 4 or 8 GB shared with the
rest of Eidolon.

The thread count already matches, so that factor is accounted for. Per-core speed is
not, and this document does not have a measurement for it — no number here should be
read as a Pi 5 latency. What does transfer:

* **Resident memory**, almost exactly. A model's weights are the same size on either
  machine.
* **The ordering.** All five models run the same INT8 GEMM kernels; a slower core
  scales them together.

What does not transfer is every millisecond in the tables below. On a board where a
recall costs several times more, the model with 20 ms of measured p95 has room and
the one with 704 ms has none — which is the same conclusion the Mac numbers reach,
arrived at with more margin.

``embedding.threads`` caps ONNX Runtime's intra-op threads. On four cores
that also run the NATS subscriber, the steward's HTTP calls and Chroma, leaving it
at the native default is a choice to re-examine on the board rather than inherit.

### Five embedders, end to end: the embedder is not the constraint

| model | RSS | probe top-1 | probe top-5 | **correct** | recall p50 | recall p95 |
|---|---|---|---|---|---|---|
| **bge-small-zh** | **130 MB** | 27 / 24 | 36 / 35 | **21/49** ×2 | 19–26 ms | **20 ms** |
| bge-base-zh | 250 MB | 26 / 24 | 39 / 36 | 20/49 | 46 ms | 72 ms |
| gte-multilingual-base | 968 MB | **28 / 29** | 35 / 36 | 20/49 | 29 ms | 40 ms |
| multilingual-e5-large | 1541 MB | 28 / 27 | **41 / 40** | 20/49 | 51 ms | 68 ms |
| embeddinggemma | 1532 MB | 27 / 27 | 36 / 37 | **23/49** | 575 ms | 704 ms |

Five models spanning 130 MB to 1.5 GB, 1.3 ms to 59 ms per call, and 384 to 1024
dimensions produce **20 to 23 correct answers out of 49**. The whole spread is three
answers, and two of those come from the model nobody can deploy.

**Neither probe metric predicts the pipeline, and both point the wrong way.** The
best probe top-1 (gte, 28/29) scored 20. The best probe top-5 (e5-large, 41/40, the
only gap in this comparison that stood outside the corpus swing) scored 20. The
model near the bottom of both scored 21. So the probe measures retrieval in
isolation and the pipeline does not inherit the ranking — using it to shortlist
candidates, which is what happened here, was not supported.

The probe is still the right instrument for the two things it measures directly and
repeatably: resident memory and per-call latency.

**Default: bge-small-zh.** Best of the four deployable options on the only metric
that ranks them, at a fifth to a twelfth of the memory and a third to a
thirty-fifth of the recall latency. embeddinggemma's two extra answers cost 704 ms
p95 — 3.5× the 200 ms chat target and 11× the 60 ms voice target — and 1.5 GB.

**And the 26–29 queries nobody answers are not an embedding problem.**
`abstention` is 0/5 and `future_plans` 0/3 under *all five* models, identically.
`topic` is 1–2 of 8 under all five. Those ask for a time range, the commitments
ledger, and an aggregate over several fragments. No embedder answers them, so the
remaining work is in recall completeness, not in model selection — which is where
this line of investigation ends.

### The 15× latency amplification claimed here was wrong

An earlier version of this section reported that a recall costs about 15× the
embedder's single-call p95, "structural rather than noise" because it held across a
45× spread of model speeds. It was fitted on two points and roughly matched by a
third:

| model | query p95 | recall p95 | ratio |
|---|---|---|---|
| bge-small-zh | 1.3 ms | 20.3 ms | 15.6× |
| bge-base-zh | 4.7 ms | 72.2 ms | 15.4× |
| **multilingual-e5-large** | **18.5 ms** | **67.8 ms** | **3.7×** |
| embeddinggemma | 59.2 ms | 704 ms | 11.9× |

The linear fit predicted 283 ms for e5-large. It measured 68 ms. A recall makes a
variable number of embedding calls depending on which channels a query triggers, so
there is no usable multiplier — only a monotone relationship: the fastest embedder
gave the fastest recall and the slowest gave the slowest.

### Qwen3-Embedding-0.6B — measured, then rejected

Asked for by name, so measured rather than reasoned about. It is a decoder embedder
(last-token pooling, instruction-aware queries), not a bigger BGE, and its top-5 is
the highest single figure anyone here produced — on one of the two corpora.

| corpus | no instruction | with instruction |
|---|---|---|
| A (35 fragments) | top-1 22, top-5 **39** | top-1 20, top-5 37 |
| B (36 fragments) | top-1 21, top-5 35 | top-1 22, top-5 **40** |

Its documented query instruction gained 5 on corpus B and lost 2 on corpus A. So
even the prefix's effect is inside the noise, and 35–40 is the same band as
everything else.

Cost is not in the noise: **+1088 MB resident and 12–33 ms per call** — 8× the
memory and 12–30× the latency of bge-small. 1.1 GB alone excludes it from a 4 GB
Raspberry Pi running other services.

Rejected on the same principle as bge-large: among models that cannot be
distinguished, take the cheapest. The support added for it — a "last" pooling mode
and a 56-tensor KV feed — was reverted rather than left in the shipped adapter,
because a pooling mode no model uses reads as a capability and is dead code. The
measurement stays reproducible in
`benchmarks/suites/probe_qwen3_embedding.py`, which carries its own decoder feed.

Telling the three BGE sizes apart needs several hundred queries rather than 43.
That is a concrete reason to run the public suites, separate from comparing
against MemPalace.

**Two queries are missed by all five**, including the 3 GB one: 我跟客户吵架了 and
我最近工作压力大吗. Four more are missed by every deployable candidate:
我以后想做什么, 我计划去哪里, 我什么时候开心, 上周聊了什么. None of these are
embedding failures — they ask for a time range, the commitments ledger, or an
aggregate over several fragments. No embedder choice fixes them, which is worth
knowing before spending more on embedders.

---

### The same pipeline at three embedders

Measured end to end, not on stored vectors — a full run each: publish 40 turns,
wait for the consumer backlog to clear, then query through the live service.

| embedder | correct | p50 | p95 | RSS | fragments stored |
|---|---|---|---|---|---|
| minilm | 11/49 (22.4%) | — | 92 ms | 381 MB | 39 |
| embeddinggemma | 23/49 (46.9%) | 575 ms | 704 ms | 3 GB | 39 |
| **bge-small-zh** | 21/49 (42.9%) | **26 ms** | **30 ms** | **133 MB** | 35 |

Two answers behind embeddinggemma, at a twenty-fourth of the p95 and a
twenty-third of the memory. The p95 also beats minilm's, because per-call latency
is 0.6 ms against 17.5.

**Run-to-run variance on this metric is zero, measured rather than assumed.** Two
runs at identical configuration, five hours apart:

| | run A | run B |
|---|---|---|
| fragments at query time | 35 | 36 |
| triples | 36 | 33 |
| ingest | 1286 s | 1442 s |
| **correct** | **21/49** | **21/49** |
| R@5 | 34/44 | 35/44 |
| per-query flips | — | **0** |

The corpus differed and the score did not move at all; one query
(``我最近工作压力大吗``) gained a vector hit without becoming correct.

That weakens a caveat stated here earlier. The embeddinggemma run had 39 fragments
against bge's 35, and this document previously used that to say the two-answer gap
might be an artefact. On the evidence it is not: the score was insensitive to the
corpus difference we did observe. A four-fragment difference is larger than the
one measured, so the caveat is reduced rather than removed — but the gap should be
read as real.

It also means the harness can attribute a change to a change, which is what makes
it usable as a regression gate rather than an indicator.

The latency difference between embedders is not subject to any of this.

**One prediction from the offline probe was confirmed**: `abstention` scored 0/5
and `future_plans` 0/3 under *both* embedders, exactly as the shared-miss analysis
said they would. Those two categories are not an embedder problem, and no
embedder choice will move them.

## What is not measured yet

Stated so the gaps are visible rather than implied by absence:

- **Quality suites.** Datasets need ingesting and the steward-mode runs cost LLM
  calls. Blocked behind a real defect first: the steward currently extracts far
  fewer graph triples than the pipeline expects (two e2e tests fail on it), so any
  graph-dependent quality number would measure that bug rather than the design.
- **Cloud anything.** There is no cloud shape any more; the implementations were
  removed. Nothing here is a cloud figure.
- **Multi-space concurrency.** One process can now serve several spaces; how p95
  degrades as it does is the number that matters for density and is not yet
  measured.
- **Public suites.** LongMemEval, LoCoMo, ConvoMem and MemBench are aligned on
  method but not yet run. The in-house 49-query set is what every figure here comes
  from.
