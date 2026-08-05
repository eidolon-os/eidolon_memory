# Benchmarks

A number here is worth something only if someone else can get it again. So every
run records what produced it — the commit, the machine, the embedder, which
backend — and writes that beside the measurements rather than in a commit
message.

## Two kinds of measurement, kept apart

They answer different questions and have different dependencies, and mixing them is
how a table comes to compare things that are not comparable.

**Model evaluation — `benchmarks/suites/`.** Does an *encoder* retrieve well? No
palace, no NATS, no steward. Two of these import nothing from `eidolon.memory` at
all, deliberately: they compare models against another project's published figures,
so a number must be attributable to a model rather than to whatever shape our
adapter had that day. A cross-project comparison that moves when we rename a method
is not a comparison.

**Service benchmarks — `scripts/benchmark/`.** Does the *service* answer well and
fast? These spawn an agent, publish turns over JetStream, wait for the LLM steward
and query through MCP, so they depend on the whole stack. `manifest.py` and
`preflight.py` live there and are shared by both.

## Layout

```
benchmarks/
  suites/                       model evaluation, runnable on a bare machine
    bench_cmteb_retrieval.py    Chinese retrieval, C-MTEB — the task we actually run
    bench_longmemeval.py        English sessions, LongMemEval + MemPalace's scorer
    probe_embedders.py          our 49-query fixture, through the shipping adapter
    probe_qwen3_embedding.py    a decoder embedder, kept for reproducibility
  upstream/                     MemPalace's split file and harness, vendored
  data/                         datasets (gitignored — 305 MB)
  runs/                         raw per-run output (gitignored)
  results/<topic>/              committed: the findings a decision rests on
    embedder-selection/
      REPORT.md                 what was decided and why
      results.json              aggregates + provenance for every run
      per-query.json            every query's rank, for paired tests
  baselines/                    regression baselines
  manifest.schema.json          provenance schema, additionalProperties: false
  BENCHMARKS.md                 published results, including corrections
```

`runs/` is ephemeral, `results/` is committed. Several figures in the embedder
investigation became unauditable because they only ever existed as console output —
two runs of the same configuration differed by 1.0pp and the difference could not be
localised, since neither file recorded per-query results or the parameters.

## Running them

```bash
# Chinese retrieval — the one that ranks encoders for this service
uv run python benchmarks/suites/bench_cmteb_retrieval.py --model bge-base-zh

# English. Reproduce MemPalace's own baseline first, as a harness self-check
uv run python benchmarks/suites/bench_longmemeval.py --arm upstream --split all
uv run python benchmarks/suites/bench_longmemeval.py --arm ours --model bge-base-zh --split dev

# On the deployment board: memory and latency, no dataset needed
uv run python benchmarks/suites/probe_embedders.py --models bge-small-zh,bge-base-zh

# Service, end to end (needs nats-server -js and an LLM endpoint)
uv run python scripts/benchmark/bench_memory_retrieve_quality.py
```

Datasets are not committed. Each suite names the exact file to fetch and which one —
LongMemEval's original is deprecated in favour of `longmemeval_s_cleaned.json`, and
the two are not comparable.

`run_suite --suite <name>` is in the plan and not yet written; the suites above are
invoked directly.

## What a manifest has to record

Not bureaucracy — each field is one way a number can turn out to mean something
different than it appears:

| Field | Why it changes the result |
|---|---|
| `git_sha`, `dirty` | A dirty tree means the code is not the commit |
| `machine` (cpu, cores, memory) | Latency is mostly a property of the host |
| `embedder` | minilm and embeddinggemma retrieve differently; a quality number without it is meaningless |
| `vector_backend`, `kg_backend` | A file and a server have different latency floors |
| `dataset`, `split`, `seed` | Especially for quality: a number on the tuning split is not a number on held-out data |
| `command` | So the run can be repeated exactly |

The embedder is read from the palace rather than from configuration, because
what matters is the one the index was built with — that is the mistake that made
an earlier quality benchmark measure minilm while production ran
embeddinggemma.

## Regression comparison

`baselines/` holds selected runs. A suite compares against the matching baseline
and fails on a **>20% p95 regression** or a **>1pp drop in R@5**. The thresholds
are loose on purpose: they catch a change in kind, not machine-to-machine
variation, and a tighter gate would be ignored within a week.

## Quality suites and honesty

Retrieval recall and end-to-end answer accuracy are different measurements, and
putting one next to the other is how benchmark tables mislead. Both are reported,
labelled, never mixed.

Tuning happens on a small dev split; the published number comes from held-out
data. MemPalace does this and says why, and it is the right practice: a figure
reached by inspecting the failures is a figure about those failures.

## Read the provenance before trusting a number

* **`code.dirty: true`** — the run did not come from the recorded commit, so it
  cannot be a baseline.
* **`params`** — everything that moves the number. `batch_size` changes padding and
  therefore the order of a float summation, so the same script can give different
  results; a figure without it is not reproducible.
* **`machine`** — every latency here is from a 12-core Apple Silicon laptop, while
  the deployment target is a four-core Raspberry Pi 5. Ordering and resident memory
  transfer; milliseconds do not.

One measurement that is easy to get wrong: **resident memory depends on input
length, not only on weights.** Activations scale linearly in batch and roughly
quadratically in sequence length, so the same encoder measured on 500-token
documents and on our 21-token fragments differs by an order of magnitude. State
which regime a memory figure came from.

See `BENCHMARKS.md` for results and their caveats, and
`results/embedder-selection/REPORT.md` for the encoder decision.
