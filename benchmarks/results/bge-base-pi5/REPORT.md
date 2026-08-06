# bge-base-zh on the Raspberry Pi 5 — measured

One encoder, characterised on the board it will actually run on, after the harness
itself was checked. Written 2026-08-06 on `refactor/memory-contracts-v2`, tree dirty
(12 uncommitted files) — so **these numbers must not be used as a regression
baseline**; `code.dirty: true` in `results.json` records it.

Raw data: `results.json`. Every figure below came from a Raspberry Pi 5 (4 cores,
7.9 GB, **no swap**, `OMP_NUM_THREADS=4`), not from a laptop. That distinction has
mattered here before: `BENCHMARKS.md` states plainly that no latency in it should
be read as a Pi 5 latency.

## The answer

**bge-base-zh is sound on this board, and the harness that says so was checked
first.** No silent truncation at the granularity we retrieve at, deterministic
across repeat runs, and correctly configured — the one configuration question
still open (BGE's query instruction) was measured and confirmed to be better left
off.

**bge-m3 is rejected on resident memory, not on quality.** It needs 2.79 GB at
batch 8 and 6.7 GB at batch 16 on an 8 GB board that also runs Chroma, NATS and the
steward. Measuring it cost two OOM kills and an unclean reboot.

## First: is the harness trustworthy?

Five ways a figure could have been wrong without anything being raised. The slice is
C-MTEB VideoRetrieval cut to 1400 documents and 200 queries, each query having
exactly one correct answer inside the pool — small enough to repeat six times,
which is the point.

| Check | Result | Reading |
|---|---|---|
| **Determinism** | two runs, **identical per-question ranks**, MRR 0.941548 both | a difference between runs is a real change, not jitter |
| **Batch size** | MRR spread **0.0077** across batch 16/32/64; **9/200** questions change rank between 16 and 64 | **the noise floor. A gap under ~0.008 MRR is not the model** |
| **BGE query instruction** | 0.9415 → **0.9332** with the prefix (**−0.0084**) | it hurts; production omits it, correctly |
| **Scoring sanity** | a document whose text equals the query ranks **1** | the scorer is not inverted or misaligned |
| **Truncation at 512** | see below | complete documents at turn granularity |

### Truncation — the check that mattered most

The tokenizer truncates at 512 tokens for every model. Whether that discards
anything is a property of the corpus, so it was measured rather than assumed.

| corpus | median | p99 | max | over 512 |
|---|---|---|---|---|
| cmteb/VideoRetrieval | 20 | 185 | 3273 | 63 / 100930 (**0.06%**) |
| cmteb/EcomRetrieval | 32 | 60 | 74 | 0 (**0.00%**) |
| cmteb/MedicalRetrieval | 104 | 489 | 514 | 403 / 100999 (**0.40%**) |
| locomo/turn | 42 | 114 | 156 | 0 (**0.00%**) |
| clongeval/turn | 44 | 112 | 327 | 0 (**0.00%**) |
| **locomo/session** | **900** | 1773 | 1965 | **268 / 272 (98.53%)** |

**At turn granularity nothing is lost** — every retrieval figure below is measured
on whole documents.

**At session granularity almost everything is.** 98.5% of LoCoMo sessions exceed the
cap, median 900 tokens against a 512 limit, so a session-granularity score measures
roughly the first half of each session. That is the granularity MemPalace publishes
at, which makes a like-for-like comparison against them a chunking problem before it
is a model problem.

### What the batch-size finding costs

0.0077 MRR of spread from padding alone, with 9 of 200 questions changing rank,
means **`batch_size` has to be held constant across anything being compared**, and
differences smaller than that are not attributable to the encoder. The suites record
it in `params` for exactly this reason; this is the first measurement of how large
the effect is.

## Retrieval quality

All runs: full corpora, batch 32, complete documents, on the Pi.

| suite | language | unit | n | R@1 | R@5 | R@10 | MRR@10 | ms/doc |
|---|---|---|---|---|---|---|---|---|
| C-MTEB VideoRetrieval | zh | document | 1000 q / 20k | **70.9%** | **89.3%** | 92.6% | **0.787** | 32.5 |
| C-MTEB EcomRetrieval | zh | document | 1000 q / 20k | 64.2% | 84.7% | 89.2% | 0.728 | 46.0 |
| CLongEval | zh | turn | 317 q | 25.2% | 63.7% | 75.1% | 0.407 | 48.2 |
| LoCoMo | en | turn | 1977 q | 14.2% | 30.3% | 39.3% | 0.211 | 50.3 |

**Chinese short-document retrieval is where it is strong** — 0.787 and 0.728 MRR
across two unrelated domains (video titles, e-commerce), which is the task the
service actually runs.

**Chinese dialogue is a different and much harder task**, and the drop is the task,
not a defect: the candidate pool is a median 289 turns from *the same conversation*,
all mutually similar, against 20 000 unrelated short titles in C-MTEB. 0.407 MRR
means the right turn is usually inside the top three.

**English is where it fails, and that is expected.** `bge-*-zh` is a Chinese-only
encoder; 0.211 on LoCoMo is what asking it English questions produces. Scaling does
not fix it — across the zh family, 24M → 326M parameters moves LoCoMo R@1 only
between 14.2% and 16.7%. **If the palace holds a meaningful amount of English, this
model is the wrong choice and no size of it is the right one.**

### The CLongEval number rests on weaker labels than the others

`label_source: derived`. CLongEval ships an answer string and no evidence pointer,
so the evidence turn is found by substring match — 41 of 358 questions had no
locatable answer and were dropped rather than scored as misses. LoCoMo's labels are
annotated; C-MTEB's are qrels. Read the 0.407 as the softest figure in the table.

## Cost on this board

| | |
|---|---|
| on disk | 198 MB |
| dimension | 768 |
| throughput | 32–50 ms/doc depending on document length |

Resident memory and single-call p50/p95 on the Pi were queued and **not reached** —
see below.

## Why bge-m3 was dropped

Not on quality. On the two checks that completed before it was stopped it looked
ordinary — deterministic (MRR 0.891006 twice), batch spread 0.0071 with 14/200 rank
flips, both in the same band as bge-base.

It was dropped because of what it costs to run here:

| | bge-base-zh | bge-m3 |
|---|---|---|
| on disk | 198 MB | 1117 MB |
| resident, batch 8, fresh process | — | **2.79 GB** |
| resident, batch 16 | — | **6.7 GB** |

An 8 GB board with no swap, shared with Chroma, NATS and an LLM steward, cannot give
an encoder 3 GB. Measuring it was not free either: two processes were OOM-killed on
2026-08-06 at ~6.6 GB anon-rss, and the board took at least one unclean reboot under
the pressure. Its earlier full-sweep result on Chinese dialogue (MRR 0.348) was also
the second-worst of eight models, so nothing is being given up.

## What is not measured, and why

Stated so the gaps are visible rather than implied by absence. The run was paused
with three items outstanding. Each writes incrementally, so it resumes rather than
restarts.

**The board did not crash, though it looked like it had.** For roughly forty minutes
it answered no ping and refused TCP on 22, and this document said at first that it
had hard-crashed. `uptime` afterwards read 2 h 34 m: it had been up the whole time,
with all four cores at 397% on MedicalRetrieval and `sshd` unable to get scheduled
long enough to finish a handshake. **On a four-core board a saturating benchmark is
indistinguishable from a dead machine from the outside**, which is worth knowing
before power-cycling one.

| Missing | Status |
|---|---|
| C-MTEB MedicalRetrieval | still running when the sweep was paused |
| Pi resident memory + query p50/p95 | queued behind it in the same script |
| LoCoMo at session granularity | never launched — and with 98.5% of sessions over the cap it would measure the truncation as much as the model |

The MemPalace comparison depends on that last row and on a chunking decision, so it
is not attempted here. Their published LoCoMo R@5 of 83.7% is scored over whole
sessions, roughly 29 candidates; every dialogue figure above is scored over
individual turns, a median 289 to 663 candidates. **The two are not the same task and
the numbers do not belong in one column.**

## Three practices this run argues for

1. **Write every check the moment it has an answer.** The first version of the
   reliability probe buffered stdout and saved once at the end; it ran two hours on
   the Pi and left nothing behind when it was stopped. The rewrite that saves per
   check survived a reboot with four results intact.
2. **Give a memory-hungry model its own batch ladder.** A single ladder of
   (16, 32, 64) is fine for bge-base and fatal for bge-m3.
3. **Swap, or a memory ceiling.** This board has neither. An encoder that briefly
   wants 6 GB takes down sshd with it, and the run becomes unreachable rather than
   merely slow.
