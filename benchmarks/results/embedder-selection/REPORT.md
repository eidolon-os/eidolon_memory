# Embedder selection — measured

Ten encoders, ranked for the task this service actually runs: retrieving short
Chinese fragments. Written 2026-08-05 on `refactor/memory-contracts-v2`, tree dirty
(53 uncommitted files) — so **these numbers must not be used as a regression
baseline**; that is what `code.dirty: true` in `results.json` records.

Raw data: `results.json` (aggregates + provenance), `per-query.json` (every query's
rank, for paired tests and flip analysis).

## The answer

**Change the default from `bge-small-zh` to `bge-base-zh`.**

| | bge-small-zh (current) | **bge-base-zh** | delta |
|---|---|---|---|
| Chinese R@1 | 67.8% | **70.7%** | **+2.9pp (≈2σ)** |
| Chinese R@5 | 86.2% | **89.0%** | +2.8pp |
| MRR@10 | 0.756 | **0.785** | +0.029 |
| resident | 130 MB | 250 MB | +120 MB (1.5% of an 8 GB Pi) |
| query p95 | 1.3 ms | 4.7 ms | +3.4 ms |

`bge-large-zh` scores 3pp higher again for 2.6× the memory and 2.7× the latency.
`gte-multilingual-base` matches `bge-large` on Chinese at 5.2 ms — the only model
strong in both languages — but costs 968 MB.

**Not yet verified end to end.** A probe advantage has failed to transfer once
already (see "What went wrong", #3), so an end-to-end run is the last gate before
switching. Switching requires deleting `palaces_root`: Chroma persists the embedder
on the collection and the width changes 512 → 768.

## Chinese — C-MTEB VideoRetrieval

1000 queries, 20k corpus pool, one relevant document each. Document median 24
characters against our fragments' 20; **no truncation for any tokenizer**. Binomial
sd = 1.49pp.

| model | R@1 | R@5 | R@10 | MRR@10 | resident | query p95 |
|---|---|---|---|---|---|---|
| bge-large-zh | **73.7%** | **90.9%** | 94.2% | **0.813** | 655 MB | 12.7 ms |
| gte-multilingual-base | 73.3% | 90.9% | 94.0% | 0.807 | 968 MB | 5.2 ms |
| **bge-base-zh** | **70.7%** | 89.0% | 92.4% | 0.785 | **250 MB** | **4.7 ms** |
| bge-small-zh *(current)* | 67.8% | 86.2% | 90.5% | 0.756 | **130 MB** | **1.3 ms** |
| multilingual-e5-large | 60.5% | 80.9% | 86.7% | 0.692 | 1541 MB | 18.5 ms |
| bge-m3 | 60.5% | 80.5% | 86.5% | 0.693 | 1542 MB | — |
| multilingual-e5-small | 60.1% | 79.1% | 85.4% | 0.684 | 180 MB | — |
| multilingual-e5-base | 57.9% | 77.4% | 84.0% | 0.669 | 840 MB | — |
| embeddinggemma | 42.4% | 60.7% | 68.9% | 0.502 | 1532 MB | 59.2 ms |
| minilm | 10.4% | 18.8% | 24.4% | 0.139 | 380 MB | 17.6 ms |

`bge-small → base → large` rises monotonically on every metric; small→large is
5.9pp ≈ 4σ. **The current default is the weakest of the four.**

## English — LongMemEval, and why it must not decide this

| model | English R@5 | Chinese R@1 |
|---|---|---|
| multilingual-e5-small | **100%** | 60.1% |
| multilingual-e5-large | **100%** | 60.5% |
| bge-m3 | 98% | 60.5% |
| gte-multilingual-base | 98% | 73.3% |
| embeddinggemma | 98% | **42.4%** |
| minilm | 96% | **10.4%** |
| bge-large-zh | 92% | **73.7%** |
| bge-base-zh | 88% | 70.7% |
| bge-small-zh | **76%** | 67.8% |

**The rankings are close to inverted.** The English winner (`multilingual-e5-small`,
100%) is third from last in Chinese. `minilm` is 96% in English and 10.4% in
Chinese.

The confound is measured, not suspected: LongMemEval's session documents run to a
median 345 tokens, and **25–26% exceed 512** for a 21k Chinese vocabulary against
9% for XLM-R's 250k, because the Chinese vocabulary needs ~30% more tokens for the
same English text. 512 is BERT's `max_position_embeddings`, not a parameter, so a
Chinese-specialised model cannot see past it. Our own fragments are median 21
tokens, longest 35, **never truncated**.

The harness is nonetheless trustworthy, with two independent checks: `--arm
upstream` reproduces MemPalace's published 96.6% R@5 exactly (NDCG 0.888 against
their 0.889), and the same model scored 96.0% / 96.6% through two different code
paths. The harness is right; the task is the wrong one for this decision.

## Raspberry Pi 5

Latency above is a 12-core Apple Silicon machine at `OMP_NUM_THREADS=4`. The thread
count already matches the Pi 5's four Cortex-A76 cores; per-core speed does not, and
there is no measurement of it here. **Re-measure on the board:**

```
uv run python benchmarks/suites/probe_embedders.py --models bge-small-zh,bge-base-zh,gte-multilingual-base
```

What transfers: resident memory (weights are the same size anywhere) and the
ordering. What does not: every millisecond.

**Resident memory depends on input length, not only on weights** — measured with
synthetic tensors so batch and length are separable:

| | weights | inference peak |
|---|---|---|
| bge-base, batch 32, 512 tokens | 175 MB | 2058 MB |
| bge-base, batch 32, 256 tokens | 175 MB | 636 MB |
| bge-base, batch 32, 128 tokens | 175 MB | 219 MB |
| bge-base, batch 8, 512 tokens | 175 MB | 512 MB |

Weights are constant; activations scale linearly in batch and roughly quadratically
in length (attention is `B × heads × L × L` per layer — 400 MB for one layer at
batch 32 × 512). Our fragments are 21 tokens, so `(21/512)² ≈ 1/600` of that term:
the 130–250 MB figures are the deployment-relevant ones.

**The only effective lever is batch size, and no config field exposes it.**
`embedding.threads` caps CPU, not memory. ORT's own switches are not levers:
disabling `enable_cpu_mem_arena` made peak *worse* (4188 MB vs 4025), and
`enable_mem_pattern=False` saved 3%.

`qwen3-embedding-0.6b` is excluded on cost alone: **22.5 s per LongMemEval question
and 9.97 GB**, against 130 MB and sub-millisecond for bge-small. Its KV cache is
returned as 56 `present.*` graph *outputs* — batch 64 × 8 heads × 512 × 128 × 4 B ×
56 ≈ 7.5 GB, which is the 9.9 GB. Empty inputs are cheap; the outputs are not.

## What went wrong, and what it changes

Five conclusions in this investigation were wrong, all the same shape: **a number
read without checking what else changed.**

1. *"bge-base has the best top-1."* It was ±3 corpus swing; the ranking inverted on
   a second corpus.
2. *"A recall costs 15× the embedder's single-call p95, structurally."* Fitted on
   two points, roughly matched by a third, refuted by the fourth —
   multilingual-e5-large predicted 283 ms, measured 68 ms.
3. *"multilingual-e5-large is one answer behind end to end."* Its second run tied,
   flipping five queries. The attribution rested on zero flips for *one* model,
   which does not establish stability for others.
4. *"bge-base's 66 ms/document is padding waste."* The 7.6× padding arithmetic is
   right, but the slow run was CPU contention from a second sweep — with sorting
   toggled and nothing else running, sorted and unsorted both take 14 s.
5. *"The English table means the recommendation has failed."* The English table
   cannot decide this at all, for the truncation reason above.

The reliability practices adopted from MemPalace's own BENCHMARKS.md, after #4 and
#5 made the earlier files unauditable:

* **Provenance** in every result: commit, dirty flag, branch, Python, mempalace
  version, machine, full command, and every parameter that moves the number.
  Reuses `scripts/benchmark/manifest.py`, which existed and these suites were not
  using.
* **Per-query detail**, their "inspect every individual answer — not just the
  aggregate". Without it, two runs of the same config differing by 1.0pp R@1 could
  not be localised — and that is exactly what happened.
* **`batch_size` recorded**, because padding changes float summation order, so the
  same script can give different numbers. "Does length-sorted batching change R@k?"
  was unanswerable from the files on disk; measured after the fix, it does not.
* **Length-sorted batching as a flag**, so its effect is measurable rather than
  assumed.
* **Held-out discipline**, theirs: dev 50 for iteration, held-out 450 touched once.
  Declared honestly — `--split all` was used for the baseline reproduction (clean,
  nothing was tuned on it), but the harness was later iterated against dev 50, so
  **a held-out figure reported now would be contaminated.**
